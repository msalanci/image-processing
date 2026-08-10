# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# Secrets Manager containers for the review editor's Basic Auth credentials and the GitHub token.
# Only the container is managed here; the value is set out-of-band, because anything Terraform manages is stored in plaintext in the state file.

resource "aws_secretsmanager_secret" "review_basic_auth" {
  name                    = "buildercards/review-basic-auth"
  description             = "HTTP Basic Auth credentials (JSON username+password) for the BuilderCards review editor Lambda."
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret" "github_pat" {
  name        = "buildercards/github-pat"
  description = "Fine-grained GitHub PAT (Contents write, single repo) for the committer Lambda's direct commits — publish and delete."

  recovery_window_in_days = 0
}
