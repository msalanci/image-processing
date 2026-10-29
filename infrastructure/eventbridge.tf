# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# Email on each Amplify deploy outcome: an EventBridge rule matching one Amplify app and branch, its
# target on the deploy-notification SNS topic, and the topic policy that lets the rule publish.
# Amplify sends these events only to the default bus of the app's own region, so var.aws_region
# must be the Amplify app's region. The Amplify app itself is not managed by Terraform.

data "aws_iam_policy_document" "deploy_notifications_publish" {
  statement {
    sid     = "AllowEventBridgePublish"
    effect  = "Allow"
    actions = ["sns:Publish"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    resources = [aws_sns_topic.deploy_notifications.arn]
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.amplify_deploy.arn]
    }
  }
}

resource "aws_cloudwatch_event_rule" "amplify_deploy" {
  name        = "BuilderCardsAmplifyDeployStatus"
  description = "Amplify deploy SUCCEED/FAILED for the BuilderCards app+branch -> SNS3 email."

  event_pattern = jsonencode({
    source        = ["aws.amplify"]
    "detail-type" = ["Amplify Deployment Status Change"]
    detail = {
      appId      = [var.amplify_app_id]
      branchName = [var.amplify_branch_name]
      jobStatus  = ["SUCCEED", "FAILED"]
    }
  })
}

resource "aws_cloudwatch_event_target" "amplify_deploy_sns" {
  rule      = aws_cloudwatch_event_rule.amplify_deploy.name
  target_id = "BuilderCardsDeploySns"
  arn       = aws_sns_topic.deploy_notifications.arn

  input_transformer {
    input_paths = {
      status = "$.detail.jobStatus"
      branch = "$.detail.branchName"
      job    = "$.detail.jobId"
      app    = "$.detail.appId"
      region = "$.region"
      time   = "$.time"
    }
    # The escaped outer quotes must stay: the template has to render to a valid JSON string,
    # which SNS then delivers as the plain-text email body.
    input_template = "\"Amplify deploy <status>: branch <branch>, job <job> (app <app>, region <region>) at <time>.\""
  }
}

resource "aws_sns_topic_policy" "deploy_notifications" {
  arn    = aws_sns_topic.deploy_notifications.arn
  policy = data.aws_iam_policy_document.deploy_notifications_publish.json
}
