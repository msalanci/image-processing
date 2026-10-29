# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# Secrets Manager containers for the review editor's HTTP Basic Auth credentials (JSON
# username and password) and the committer's fine-grained GitHub PAT. Terraform manages
# only the containers, never a secret version: anything Terraform manages is stored in
# plaintext in terraform.tfstate. The values are set with `aws secretsmanager
# put-secret-value` after apply, and each Lambda fails closed until its value is set.

resource "aws_secretsmanager_secret" "review_basic_auth" {
  name        = "buildercards/review-basic-auth"
  description = "HTTP Basic Auth credentials (JSON username+password) for the BuilderCards review editor Lambda."

  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret" "github_pat" {
  name        = "buildercards/github-pat"
  description = "Fine-grained GitHub PAT (Contents write, single repo) for the committer Lambda's direct commits — publish and delete."

  recovery_window_in_days = 0
}
