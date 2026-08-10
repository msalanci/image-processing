# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# IAM roles

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "get_upload_url" {
  name               = "BuilderCardsGetUploadUrlRole"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "get_upload_url_basic" {
  role       = aws_iam_role.get_upload_url.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "get_upload_url_presign" {
  name = "BuilderCardsPresignS3"
  role = aws_iam_role.get_upload_url.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "arn:aws:s3:::${var.images_bucket_name}/${var.raw_prefix}*"
      }
    ]
  })
}

resource "aws_iam_role" "image_processor" {
  name               = "BuilderCardsImageProcessorRole"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "image_processor_basic" {
  role       = aws_iam_role.image_processor.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "image_processor_s3" {
  name = "BuilderCardsImageProcessorS3"
  role = aws_iam_role.image_processor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadRaw"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::${var.images_bucket_name}/${var.raw_prefix}*"
      },
      {
        Sid      = "WriteCornersToRaw"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "arn:aws:s3:::${var.images_bucket_name}/${var.raw_prefix}*"
      },
      {
        Sid      = "WriteFinished"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "arn:aws:s3:::${var.images_bucket_name}/${var.finished_prefix}*"
      }
    ]
  })
}

locals {
  bedrock_fm_id = replace(var.bedrock_llm, "/^(us|eu|apac|jp|global|us-gov)\\./", "")
}

resource "aws_iam_role_policy" "image_processor_extract" {
  name = "BuilderCardsImageProcessorExtract"
  role = aws_iam_role.image_processor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DynamoWrite"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = [aws_dynamodb_table.cards.arn]
      },
      {
        Sid    = "BedrockInvoke"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel"]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/${local.bedrock_fm_id}",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_llm}",
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "image_processor_sns" {
  name = "BuilderCardsImageProcessorSnsPublish"
  role = aws_iam_role.image_processor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.upload_notifications.arn
      }
    ]
  })
}

resource "aws_iam_role" "review_editor" {
  name               = "BuilderCardsReviewEditorRole"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "review_editor_basic" {
  role       = aws_iam_role.review_editor.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "review_editor_ddb" {
  name = "BuilderCardsReviewEditorDynamo"
  role = aws_iam_role.review_editor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ]
        Resource = [
          aws_dynamodb_table.cards.arn,
          "${aws_dynamodb_table.cards.arn}/index/*",
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "review_editor_secret" {
  name = "BuilderCardsReviewEditorReadBasicAuth"
  role = aws_iam_role.review_editor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.review_basic_auth.arn
      }
    ]
  })
}

resource "aws_iam_role_policy" "review_editor_s3_finished" {
  name = "BuilderCardsReviewEditorReadFinished"
  role = aws_iam_role.review_editor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::${var.images_bucket_name}/${var.finished_prefix}*"
      }
    ]
  })
}

resource "aws_iam_role_policy" "review_editor_invoke_committer" {
  name = "BuilderCardsReviewEditorInvokeCommitter"
  role = aws_iam_role.review_editor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = aws_lambda_function.committer.arn
      }
    ]
  })
}

resource "aws_iam_role_policy" "review_editor_rejected_s3" {
  name = "BuilderCardsReviewEditorRejectedS3"
  role = aws_iam_role.review_editor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListRejected"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::${var.images_bucket_name}"
        Condition = {
          StringLike = { "s3:prefix" = "${var.rejected_prefix}*" }
        }
      },
      {
        Sid      = "ReadRejected"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::${var.images_bucket_name}/${var.rejected_prefix}*"
      },
      {
        Sid      = "DeleteRejected"
        Effect   = "Allow"
        Action   = ["s3:DeleteObject"]
        Resource = "arn:aws:s3:::${var.images_bucket_name}/${var.rejected_prefix}*"
      },
      {
        Sid      = "ReprocessWriteRaw"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "arn:aws:s3:::${var.images_bucket_name}/${var.raw_prefix}*"
      }
    ]
  })
}

resource "aws_iam_role" "committer" {
  name               = "BuilderCardsCommitterRole"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "committer_basic" {
  role       = aws_iam_role.committer.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "committer_github_secret" {
  name = "BuilderCardsCommitterReadGithubPat"
  role = aws_iam_role.committer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.github_pat.arn
      }
    ]
  })
}

resource "aws_iam_role_policy" "committer_ddb" {
  name = "BuilderCardsCommitterDynamo"
  role = aws_iam_role.committer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:DeleteItem",
        ]
        Resource = aws_dynamodb_table.cards.arn
      }
    ]
  })
}

resource "aws_iam_role_policy" "committer_s3" {
  name = "BuilderCardsCommitterS3"
  role = aws_iam_role.committer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadFinished"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::${var.images_bucket_name}/${var.finished_prefix}*"
      },
      {
        Sid      = "DeleteRaw"
        Effect   = "Allow"
        Action   = ["s3:DeleteObject"]
        Resource = "arn:aws:s3:::${var.images_bucket_name}/${var.raw_prefix}*"
      },
      {
        Sid      = "DeleteFinished"
        Effect   = "Allow"
        Action   = ["s3:DeleteObject"]
        Resource = "arn:aws:s3:::${var.images_bucket_name}/${var.finished_prefix}*"
      }
    ]
  })
}

resource "aws_iam_role" "card_detect" {
  name               = "BuilderCardsCardDetectRole"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "card_detect_basic" {
  role       = aws_iam_role.card_detect.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "card_detect_s3" {
  name = "BuilderCardsCardDetectS3"
  role = aws_iam_role.card_detect.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadRaw"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::${var.images_bucket_name}/${var.raw_prefix}*"
      },
      {
        Sid      = "QuarantineDeleteRaw"
        Effect   = "Allow"
        Action   = ["s3:DeleteObject"]
        Resource = "arn:aws:s3:::${var.images_bucket_name}/${var.raw_prefix}*"
      },
      {
        Sid      = "QuarantineWriteRejected"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "arn:aws:s3:::${var.images_bucket_name}/${var.rejected_prefix}*"
      }
    ]
  })
}

resource "aws_iam_role_policy" "card_detect_invoke_processor" {
  name = "BuilderCardsCardDetectInvokeProcessor"
  role = aws_iam_role.card_detect.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = aws_lambda_function.image_processor.arn
      }
    ]
  })
}

resource "aws_iam_role_policy" "card_detect_sns" {
  name = "BuilderCardsCardDetectSnsPublish"
  role = aws_iam_role.card_detect.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.reject_notifications.arn
      }
    ]
  })
}
