# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# Create EventBridge, which is need for emails after Amplify events change

resource "aws_cloudwatch_event_rule" "amplify_deploy" {
  name        = "BuilderCardsAmplifyDeployStatus"
  description = "Amplify deploy SUCCEED/FAILED for the BuilderCards app+branch -> deploy_notifications topic."

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

# The transformer reshapes the raw event into one readable line
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

    # The escaped quotes make this render as a JSON string, which SNS sends as plain text
    input_template = "\"Amplify deploy <status>: branch <branch>, job <job> (app <app>, region <region>) at <time>.\""
  }
}

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

resource "aws_sns_topic_policy" "deploy_notifications" {
  arn    = aws_sns_topic.deploy_notifications.arn
  policy = data.aws_iam_policy_document.deploy_notifications_publish.json
}
