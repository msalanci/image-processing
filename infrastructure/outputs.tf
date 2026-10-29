# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# Outputs of the stack: the images bucket and its prefixes, the upload Lambda and its API
# endpoint, the review editor URL, the ARNs of the live SNS notification topics, and the queue
# of failed upload events.

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
  description = "SNS3 Topic ARN for Amplify deploy (success/failure) notifications"
  value       = aws_sns_topic.deploy_notifications.arn
}

output "reject_notification_topic_arn" {
  description = "SNS4 Topic ARN for card-detect reject notifications"
  value       = aws_sns_topic.reject_notifications.arn
}

output "review_editor_url" {
  description = "Open this in a browser to review/approve cards (the review editor, on its own admin API)."
  value       = "${aws_apigatewayv2_api.admin.api_endpoint}/review"
}

output "failed_events_queue_url" {
  description = "URL of the SQS queue that keeps, for 14 days, the upload events card-detect or the image-processor still failed on after Lambda's retries"
  value       = aws_sqs_queue.failed_events.url
}
