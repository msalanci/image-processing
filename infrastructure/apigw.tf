# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# The two HTTP APIs: the public, unauthenticated upload API (POST /get-upload-url) and the
# review editor's admin API (/review routes), kept apart for a smaller blast radius. The admin
# API is locked by the editor's Basic Auth and CSRF check, not by its URL, and needs no CORS
# because its pages are same-origin. Every route is throttled at its stage to a rate for a few
# human users, so excess requests get 429 at the gateway without invoking the Lambda.

resource "aws_apigatewayv2_api" "upload" {
  name          = "BuilderCardsUploadApi"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = var.allowed_origins
    allow_methods = ["POST", "OPTIONS"]
    allow_headers = ["*"]
  }
}

resource "aws_apigatewayv2_integration" "upload" {
  api_id                 = aws_apigatewayv2_api.upload.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.get_upload_url.arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "upload" {
  api_id    = aws_apigatewayv2_api.upload.id
  route_key = "POST /get-upload-url"
  target    = "integrations/${aws_apigatewayv2_integration.upload.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.upload.id
  name        = "$default"
  auto_deploy = true

  # route_settings name the route by its key string, which gives Terraform no dependency on the
  # route; without this the stage can be created first and fail with NotFoundException.
  depends_on = [aws_apigatewayv2_route.upload]

  route_settings {
    route_key              = "POST /get-upload-url"
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }
}

resource "aws_apigatewayv2_api" "admin" {
  name          = "BuilderCardsAdminApi"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "review" {
  api_id                 = aws_apigatewayv2_api.admin.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.review_editor.arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "review_list" {
  api_id    = aws_apigatewayv2_api.admin.id
  route_key = "GET /review"
  target    = "integrations/${aws_apigatewayv2_integration.review.id}"
}

resource "aws_apigatewayv2_route" "review_edit" {
  api_id    = aws_apigatewayv2_api.admin.id
  route_key = "GET /review/edit"
  target    = "integrations/${aws_apigatewayv2_integration.review.id}"
}

resource "aws_apigatewayv2_route" "review_save" {
  api_id    = aws_apigatewayv2_api.admin.id
  route_key = "POST /review/save"
  target    = "integrations/${aws_apigatewayv2_integration.review.id}"
}

resource "aws_apigatewayv2_route" "review_delete_confirm" {
  api_id    = aws_apigatewayv2_api.admin.id
  route_key = "GET /review/delete"
  target    = "integrations/${aws_apigatewayv2_integration.review.id}"
}

resource "aws_apigatewayv2_route" "review_delete" {
  api_id    = aws_apigatewayv2_api.admin.id
  route_key = "POST /review/delete"
  target    = "integrations/${aws_apigatewayv2_integration.review.id}"
}

resource "aws_apigatewayv2_route" "review_reprocess" {
  api_id    = aws_apigatewayv2_api.admin.id
  route_key = "POST /review/reprocess"
  target    = "integrations/${aws_apigatewayv2_integration.review.id}"
}

resource "aws_apigatewayv2_route" "review_reject_delete_confirm" {
  api_id    = aws_apigatewayv2_api.admin.id
  route_key = "GET /review/reject-delete"
  target    = "integrations/${aws_apigatewayv2_integration.review.id}"
}

resource "aws_apigatewayv2_route" "review_reject_delete" {
  api_id    = aws_apigatewayv2_api.admin.id
  route_key = "POST /review/reject-delete"
  target    = "integrations/${aws_apigatewayv2_integration.review.id}"
}

resource "aws_apigatewayv2_stage" "admin" {
  api_id      = aws_apigatewayv2_api.admin.id
  name        = "$default"
  auto_deploy = true

  # route_settings name the routes by their key strings, which gives Terraform no dependency on
  # the routes; without this the stage can be created first and fail with NotFoundException.
  depends_on = [
    aws_apigatewayv2_route.review_list,
    aws_apigatewayv2_route.review_edit,
    aws_apigatewayv2_route.review_save,
    aws_apigatewayv2_route.review_delete_confirm,
    aws_apigatewayv2_route.review_delete,
    aws_apigatewayv2_route.review_reprocess,
    aws_apigatewayv2_route.review_reject_delete_confirm,
    aws_apigatewayv2_route.review_reject_delete,
  ]

  route_settings {
    route_key              = "GET /review"
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }
  route_settings {
    route_key              = "GET /review/edit"
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }
  route_settings {
    route_key              = "POST /review/save"
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }
  route_settings {
    route_key              = "GET /review/delete"
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }
  route_settings {
    route_key              = "POST /review/delete"
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }
  route_settings {
    route_key              = "POST /review/reprocess"
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }
  route_settings {
    route_key              = "GET /review/reject-delete"
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }
  route_settings {
    route_key              = "POST /review/reject-delete"
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }
}
