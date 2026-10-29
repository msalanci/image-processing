# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# Where an upload event goes when card-detect or the image-processor keeps failing on it: Lambda
# retries each asynchronous invocation twice, then sends the event to an SQS queue that keeps it for
# 14 days, the longest SQS allows, and an alarm emails the maintainer while an event waits there.
# Lambda checks the role's SendMessage grant (iam.tf) when it saves a destination, so each setting
# waits for its grant.

resource "aws_sqs_queue" "failed_events" {
  name                      = "BuilderCardsFailedEvents"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_cloudwatch_metric_alarm" "failed_events_waiting" {
  alarm_name          = "BuilderCardsFailedEventsWaiting"
  alarm_description   = "An upload failed in card-detect or the image-processor after every retry, and its event is waiting in the BuilderCardsFailedEvents queue. Read it, re-run it and delete it as the README's 'When an upload keeps failing' section shows; the alarm clears when the queue is empty."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  # SQS stops sending this metric for an empty queue that has been idle for 6 hours: no data means no events.
  treat_missing_data = "notBreaching"
  alarm_actions      = [aws_sns_topic.alarms.arn]
  ok_actions         = [aws_sns_topic.alarms.arn]

  dimensions = {
    QueueName = aws_sqs_queue.failed_events.name
  }
}

resource "aws_lambda_function_event_invoke_config" "card_detect" {
  function_name                = aws_lambda_function.card_detect.function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 21600

  destination_config {
    on_failure {
      destination = aws_sqs_queue.failed_events.arn
    }
  }

  depends_on = [aws_iam_role_policy.card_detect_failed_events]
}

resource "aws_lambda_function_event_invoke_config" "image_processor" {
  function_name                = aws_lambda_function.image_processor.function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 21600

  destination_config {
    on_failure {
      destination = aws_sqs_queue.failed_events.arn
    }
  }

  depends_on = [aws_iam_role_policy.image_processor_failed_events]
}
