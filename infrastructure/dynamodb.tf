# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# DynamoDB table of the cards, one item per uploaded card: the pipeline's source of truth.
# image-processor writes each item as pending, with AI drafts of the title, effect and description;
# the review editor corrects it and sets it to approved, and the committer publishes only approved
# items. The status-index GSI lets the review editor list the cards of one status without a scan.

resource "aws_dynamodb_table" "cards" {
  name         = "BuilderCards"
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "cardId"

  attribute {
    name = "cardId"
    type = "S"
  }
  attribute {
    name = "status"
    type = "S"
  }

  global_secondary_index {
    name            = "status-index"
    hash_key        = "status"
    projection_type = "ALL"
  }
}
