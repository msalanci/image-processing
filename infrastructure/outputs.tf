# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# Outputs for the images bucket, the two API endpoints and the SNS topics.

output "images_bucket_name" {
  description = "Name of the BuilderCards images bucket"
  value       = aws_s3_bucket.images.id
}

output "images_bucket_arn" {
  description = "ARN of the BuilderCards images bucket"
  value       = aws_s3_bucket.images.arn
}

output "images_bucket_raw_prefix" {
  description = "Prefix for raw card uploads"
  value       = var.raw_prefix
}

output "images_bucket_finished_prefix" {
  description = "Prefix for processed card images"
  value       = var.finished_prefix
}

output "get_upload_url_lambda_arn" {
  description = "ARN of the GetUploadUrl Lambda"
  value       = aws_lambda_function.get_upload_url.arn
}

output "get_upload_url_api_endpoint" {
  description = "HTTP API base URL (call POST /get-upload-url)"
  value       = aws_apigatewayv2_api.upload.api_endpoint
}

output "upload_notification_topic_arn" {
  description = "SNS Topic ARN for upload notifications"
  value       = aws_sns_topic.upload_notifications.arn
}

output "deploy_notification_topic_arn" {
  description = "Topic ARN for Amplify deploy (success/failure) notifications"
  value       = aws_sns_topic.deploy_notifications.arn
}

output "reject_notification_topic_arn" {
  description = "Topic ARN for card-detect reject notifications"
  value       = aws_sns_topic.reject_notifications.arn
}

output "review_editor_url" {
  description = "Open this in a browser to review/approve cards (hosted review.py, dedicated admin API)."
  value       = "${aws_apigatewayv2_api.admin.api_endpoint}/review"
}
