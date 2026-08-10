# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# Input variables, plus the account and region data sources the other files reference.

# These have no default on purpose: a fork must set them in terraform.tfvars.

variable "images_bucket_name" {
  description = "Globally-unique S3 bucket name for raw + finished card images. Set in terraform.tfvars (must be changed per fork)."
  type        = string
}

variable "notification_email" {
  description = "Email subscribed to all three notification topics — upload (go review), reject (quarantined upload) and deploy (Amplify result). Set in terraform.tfvars. Must confirm via the link AWS emails after apply."
  type        = string
}

variable "github_owner" {
  description = "GitHub repo owner/org the review editor commits published cards to (direct commit). Set in terraform.tfvars."
  type        = string
}

variable "github_repo" {
  description = "GitHub repository name the review editor commits published cards to. Set in terraform.tfvars."
  type        = string
}

variable "amplify_app_id" {
  description = "Amplify app ID (a 14-character string like d1a2b3c4e5f6g7, shown in the Amplify console) used to filter the EventBridge 'Amplify Deployment Status Change' events to THIS app. Set in terraform.tfvars. NOTE: the app must live in aws_region — EventBridge only sees Amplify events on the default bus in the app's own region."
  type        = string
}

# The rest have sensible defaults and can be overridden in terraform.tfvars.

variable "aws_region" {
  description = "AWS region to deploy into. Frontend currently points at eu-central-1."
  type        = string
  default     = "eu-central-1"
}

variable "raw_prefix" {
  description = "S3 key prefix for raw card uploads."
  type        = string
  default     = "images/raw/"
}

variable "max_upload_bytes" {
  description = "Max allowed upload size in bytes, enforced S3-side by the get-upload-url presigned POST (content-length-range). Default 26214400 = 25 MiB. The upload form mirrors this as a client-side UX check."
  type        = number
  default     = 26214400
}

variable "finished_prefix" {
  description = "S3 key prefix for processed card images (reserved for future use)."
  type        = string
  default     = "images/finished/"
}

variable "rejected_prefix" {
  description = "S3 key prefix where the card-detect gate quarantines uploads that don't look like a card. Dead-end (nothing triggers on it); add a lifecycle rule later to auto-expire it."
  type        = string
  default     = "images/rejected/"
}

variable "static_cards_dir" {
  description = "Where the finished card PNG should be copied, in your local checkout of the SITE repo. Cosmetic only — it is pasted into the notification email's cd command; nothing in this stack reads or writes it. The default matches this project's own Hugo layout; override it in terraform.tfvars to match yours."
  type        = string
  default     = "web/awsbuildercards/static/cards"
}

variable "raw_download_dir" {
  description = "Destination dir for the original-photo download command in the notification email — a path on your own machine. Cosmetic only; nothing in this stack reads or writes it. Override in terraform.tfvars."
  type        = string
  default     = "card_pictures/RAW"
}

variable "corners_download_dir" {
  description = "Destination dir for the corners-overlay download command in the notification email — a path on your own machine. Cosmetic only; nothing in this stack reads or writes it. Override in terraform.tfvars."
  type        = string
  default     = "card_pictures/CORNERS"
}

variable "bedrock_llm" {
  description = "Bedrock inference-profile ID for card-text extraction. Must be an EU profile (eu.*) since the stack runs in eu-central-1."
  type        = string
  default     = "eu.amazon.nova-2-lite-v1:0"
}

variable "bedrock_region" {
  description = "AWS region where the Bedrock model/inference-profile is invoked. Usually equals aws_region, but kept separate because the inference profile in bedrock_llm is region-specific: a fork in us-east-1 needs a us.* profile and this set to their region."
  type        = string
  default     = "eu-central-1"
}

variable "github_base_branch" {
  description = "Your live / deploy branch — the branch Amplify builds and publishes the site from (e.g. master). In the full-auto single-branch setup the committer commits straight to this branch (set github_publish_branch to the same value). Override in terraform.tfvars."
  type        = string
  default     = "master"
}

variable "github_publish_branch" {
  description = "Branch the committer Lambda commits published cards to (must already exist in the repo — the GitHub Contents API won't create it). FULL-AUTO: set this equal to github_base_branch (your live branch) so approved cards deploy with no manual merge. PR-GATED alternative: set it to a separate staging branch you merge into the live branch yourself (the live branch can then keep 'require a PR' protection). Override in terraform.tfvars."
  type        = string
  default     = "master" # full-auto out of the box: publish branch == base branch
}

variable "amplify_branch_name" {
  description = "Amplify branch whose deploys trigger the deploy-notification email (your live/deploy branch). Defaults to master. Override in terraform.tfvars."
  type        = string
  default     = "master"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
