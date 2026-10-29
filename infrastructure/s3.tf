# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# The images bucket: raw uploads, finished card images and rejected uploads under their
# key prefixes, in one private, versioned and encrypted bucket. It also holds the CORS
# rule for the browser upload form and the S3 trigger of the card-detect gate.

resource "aws_s3_bucket" "images" {
  bucket = var.images_bucket_name
}

resource "aws_s3_bucket_versioning" "images" {
  bucket = aws_s3_bucket.images.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "images" {
  bucket = aws_s3_bucket.images.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "images" {
  bucket                  = aws_s3_bucket.images.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "images" {
  bucket = aws_s3_bucket.images.id
  rule {
    object_ownership = "BucketOwnerPreferred"
  }
}

resource "aws_s3_bucket_cors_configuration" "images" {
  bucket = aws_s3_bucket.images.id
  cors_rule {
    allowed_methods = ["POST"]
    allowed_origins = var.allowed_origins
    allowed_headers = ["*"]
    max_age_seconds = 3000
  }
}

resource "aws_s3_bucket_notification" "images" {
  bucket = aws_s3_bucket.images.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.card_detect.arn
    # Browser uploads arrive as s3:ObjectCreated:Post (presigned POST) and the review
    # editor's re-submit as :Put; a narrower filter silently skips them. The .jpg suffix
    # stops the processor's <stem>__corners.png overlay in raw/ from re-triggering.
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = var.raw_prefix
    filter_suffix = ".jpg"
  }

  # S3 validates the Lambda invoke permission when it saves the notification.
  depends_on = [
    aws_lambda_permission.card_detect_s3,
  ]
}

resource "aws_s3_bucket_policy" "images" {
  bucket = aws_s3_bucket.images.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowAccountReadFinished"
        Effect    = "Allow"
        Principal = { AWS = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.images.arn}/${var.finished_prefix}*"
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.images]
}
