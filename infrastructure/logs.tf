# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# CloudWatch log groups of the five Lambda functions, which keep each function's logs for
# var.lambda_log_retention_days. Each function depends_on these groups (lambda.tf), so Lambda
# never creates its own group first and makes the create here fail. With
# var.import_existing_log_groups set, Terraform imports the log groups that Lambda created by
# itself on a stack deployed before they were managed here.

locals {
  lambda_log_group_names = { for key, name in local.lambda_function_names : key => "/aws/lambda/${name}" }
}

import {
  for_each = { for key, name in local.lambda_log_group_names : key => name if var.import_existing_log_groups }
  to       = aws_cloudwatch_log_group.lambda[each.key]
  id       = each.value
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each = local.lambda_log_group_names

  name              = each.value
  retention_in_days = var.lambda_log_retention_days
}
