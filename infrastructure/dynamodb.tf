# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# DynamoDB table

resource "aws_dynamodb_table" "cards" {
  name         = "BuilderCards"
  billing_mode = "PAY_PER_REQUEST" # on-demand

  hash_key = "cardId"

  attribute {
    name = "cardId"
    type = "S"
  }
  attribute {
    name = "status"
    type = "S"
  }

  # Lets the review editor list the cards awaiting review without scanning the table
  global_secondary_index {
    name            = "status-index"
    hash_key        = "status"
    projection_type = "ALL"
  }
}
