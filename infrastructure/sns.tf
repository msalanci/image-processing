# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# Three SNS topics and their email subscriptions: 
# - a card to review
# - a deploy result
# - a rejected upload

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
