# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# SNS topics that email the maintainer, each with one email subscription: new uploads
# to review (SNS1, published by the image-processor), Amplify deploy results (SNS3, by
# the EventBridge rule) and auto-rejected uploads (SNS4, by the card-detect gate).
# Email subscriptions cannot be confirmed automatically: each new one stays pending until
# the maintainer clicks the link in AWS's confirmation email.

resource "aws_sns_topic" "upload_notifications" {
  name         = "BuilderCardsUploadNotifications"
  display_name = "Builder Cards Upload Notifications"
}

resource "aws_sns_topic_subscription" "upload_email" {
  topic_arn = aws_sns_topic.upload_notifications.arn
  protocol  = "email"
  endpoint  = var.notification_email
}

resource "aws_sns_topic" "deploy_notifications" {
  name         = "BuilderCardsDeploymentNotifications"
  display_name = "Builder Cards Deploy"
}

resource "aws_sns_topic_subscription" "deploy_email" {
  topic_arn = aws_sns_topic.deploy_notifications.arn
  protocol  = "email"
  endpoint  = var.notification_email
}

resource "aws_sns_topic" "reject_notifications" {
  name         = "BuilderCardsRejectedUploads"
  display_name = "Builder Cards Rejected"
}

resource "aws_sns_topic_subscription" "reject_email" {
  topic_arn = aws_sns_topic.reject_notifications.arn
  protocol  = "email"
  endpoint  = var.notification_email
}
