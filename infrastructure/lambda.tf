# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# The five Lambda functions of the card pipeline and the permissions that let API Gateway and S3
# invoke them. The zip functions are packaged from lambda/<fn>/; card-detect and image-processor
# run images that ecr.tf builds. image-processor and committer have no aws_lambda_permission on
# purpose: other Lambdas' roles invoke them (iam.tf). Only the committer holds the GitHub PAT and
# the delete power; the internet-facing review editor only invokes it.

locals {
  lambda_function_names = {
    get_upload_url  = "BuilderCardsGetUploadUrl"
    image_processor = "BuilderCardsImageProcessor"
    card_detect     = "BuilderCardsCardDetect"
    committer       = "BuilderCardsCommitter"
    review_editor   = "BuilderCardsReviewEditor"
  }

  force_process_metadata_key = "force-process"
}

data "archive_file" "get_upload_url" {
  type        = "zip"
  source_file = "${path.module}/lambda/get-upload-url/index.py"
  output_path = "${path.module}/build/get-upload-url.zip"
}

data "archive_file" "review_editor" {
  type        = "zip"
  source_file = "${path.module}/lambda/review-editor/index.py"
  output_path = "${path.module}/build/review-editor.zip"
}

data "archive_file" "committer" {
  type        = "zip"
  source_file = "${path.module}/lambda/committer/index.py"
  output_path = "${path.module}/build/committer.zip"
}

resource "aws_lambda_function" "get_upload_url" {
  function_name = local.lambda_function_names.get_upload_url
  runtime       = "python3.14"
  handler       = "index.handler"
  role          = aws_iam_role.get_upload_url.arn
  timeout       = 10

  filename         = data.archive_file.get_upload_url.output_path
  source_code_hash = data.archive_file.get_upload_url.output_base64sha256

  environment {
    variables = {
      BUCKET_NAME      = var.images_bucket_name
      RAW_PREFIX       = var.raw_prefix
      MAX_UPLOAD_BYTES = tostring(var.max_upload_bytes)
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_lambda_permission" "get_upload_url_apigw" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.get_upload_url.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.upload.execution_arn}/*/*"
}

resource "aws_lambda_function" "image_processor" {
  function_name = local.lambda_function_names.image_processor
  role          = aws_iam_role.image_processor.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.image_processor.repository_url}@${data.aws_ecr_image.image_processor.image_digest}"
  architectures = ["arm64"]

  # BiRefNet on CPU needs more than 4 GB on full-size photos, and the maximum memory also gives
  # the most vCPUs.
  memory_size = 10240
  timeout     = 180

  # Caps what a flood of public uploads can cost; excess async invokes wait in Lambda's retry
  # queue.
  reserved_concurrent_executions = 2

  environment {
    variables = {
      RAW_PREFIX           = var.raw_prefix
      FINISHED_PREFIX      = var.finished_prefix
      BACKGROUND           = "black"
      DDB_TABLE            = aws_dynamodb_table.cards.name
      BEDROCK_MODEL_ID     = var.bedrock_llm
      BEDROCK_REGION       = var.bedrock_region
      SNS_TOPIC_ARN        = aws_sns_topic.upload_notifications.arn
      STATIC_CARDS_DIR     = var.static_cards_dir
      RAW_DOWNLOAD_DIR     = var.raw_download_dir
      CORNERS_DOWNLOAD_DIR = var.corners_download_dir
      REVIEW_URL           = "${aws_apigatewayv2_api.admin.api_endpoint}/review"
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_lambda_function" "card_detect" {
  function_name = local.lambda_function_names.card_detect
  role          = aws_iam_role.card_detect.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.card_detect.repository_url}@${data.aws_ecr_image.card_detect.image_digest}"
  architectures = ["arm64"]

  memory_size = 2048
  timeout     = 60

  # Caps what a flood of public uploads can cost, like the image-processor's cap.
  reserved_concurrent_executions = 5

  environment {
    variables = {
      RAW_PREFIX                    = var.raw_prefix
      REJECTED_PREFIX               = var.rejected_prefix
      IMAGE_PROCESSOR_FUNCTION_NAME = aws_lambda_function.image_processor.function_name
      REJECT_TOPIC_ARN              = aws_sns_topic.reject_notifications.arn
      REJECTED_TAB_URL              = "${aws_apigatewayv2_api.admin.api_endpoint}/review?status=rejected"
      KEEP_THRESHOLD                = "0.50"
      DROP_THRESHOLD                = "0.20"
      FORCE_PROCESS_META            = local.force_process_metadata_key
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_lambda_permission" "card_detect_s3" {
  statement_id   = "AllowS3Invoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.card_detect.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = "arn:${data.aws_partition.current.partition}:s3:::${var.images_bucket_name}"
  source_account = data.aws_caller_identity.current.account_id
}

resource "aws_lambda_function" "committer" {
  function_name = local.lambda_function_names.committer
  runtime       = "python3.14"
  handler       = "index.handler"
  role          = aws_iam_role.committer.arn
  timeout       = 30

  filename         = data.archive_file.committer.output_path
  source_code_hash = data.archive_file.committer.output_base64sha256

  environment {
    variables = {
      GITHUB_PAT_SECRET_ARN = aws_secretsmanager_secret.github_pat.arn
      GITHUB_OWNER          = var.github_owner
      GITHUB_REPO           = var.github_repo
      GITHUB_PUBLISH_BRANCH = var.github_publish_branch
      IMAGES_BUCKET         = var.images_bucket_name
      DDB_TABLE             = aws_dynamodb_table.cards.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_lambda_function" "review_editor" {
  function_name = local.lambda_function_names.review_editor
  runtime       = "python3.14"
  handler       = "index.handler"
  role          = aws_iam_role.review_editor.arn
  timeout       = 30

  # Bounds what brute-force or flood requests on the internet-facing editor can cost.
  reserved_concurrent_executions = 5

  filename         = data.archive_file.review_editor.output_path
  source_code_hash = data.archive_file.review_editor.output_base64sha256

  environment {
    variables = {
      DDB_TABLE               = aws_dynamodb_table.cards.name
      BASE_PATH               = "/review"
      BASIC_AUTH_SECRET_ARN   = aws_secretsmanager_secret.review_basic_auth.arn
      IMAGES_BUCKET           = var.images_bucket_name
      COMMITTER_FUNCTION_NAME = aws_lambda_function.committer.function_name
      RAW_PREFIX              = var.raw_prefix
      REJECTED_PREFIX         = var.rejected_prefix
      FORCE_PROCESS_META      = local.force_process_metadata_key
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_lambda_permission" "review_editor_apigw" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.review_editor.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.admin.execution_arn}/*/*"
}
