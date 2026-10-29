# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# Input variables of the stack, and the data sources for the current account, region and
# partition. Variables without a default are deployer-specific and must be set in
# terraform.tfvars; the rest can be overridden there.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

variable "images_bucket_name" {
  description = "Globally-unique S3 bucket name for raw + finished card images. Set in terraform.tfvars (must be changed per fork)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.images_bucket_name))
    error_message = "The images bucket name must be a valid S3 bucket name: 3 to 63 lowercase letters, digits, dots or hyphens, starting and ending with a letter or digit."
  }
}

variable "notification_email" {
  description = "Email subscribed to all three live notification topics — SNS1 upload (go review), SNS4 reject (quarantined upload) and SNS3 deploy (Amplify result). Set in terraform.tfvars. Must confirm via the link AWS emails after apply."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.notification_email))
    error_message = "The notification email must be a single email address, such as name@example.com."
  }
}

variable "github_owner" {
  description = "GitHub user or organization that owns the site repository; the committer Lambda commits published cards to it directly. Set in terraform.tfvars."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9-]*$", var.github_owner))
    error_message = "The GitHub owner must be a user or organization name (letters, digits and hyphens), not a URL or an owner/repo path."
  }
}

variable "github_repo" {
  description = "GitHub repository name the committer Lambda commits published cards to. Set in terraform.tfvars."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+$", var.github_repo))
    error_message = "The GitHub repository must be a bare repository name (letters, digits, dots, underscores and hyphens), not a URL or an owner/repo path."
  }
}

variable "amplify_app_id" {
  description = "ID of the Amplify app that builds the site (e.g. d1a2b3c4d5e6f7), used to filter the EventBridge 'Amplify Deployment Status Change' events to THIS app. The app is created in the Amplify console, not by this Terraform, which only references it by ID. Set in terraform.tfvars. NOTE: the app must live in aws_region — EventBridge only sees Amplify events on the default bus in the app's own region."
  type        = string

  validation {
    condition     = can(regex("^d[a-z0-9]{1,19}$", var.amplify_app_id))
    error_message = "The Amplify app ID must be the ID shown in the Amplify console: a lowercase d followed by lowercase letters and digits, at most 20 characters."
  }
}

variable "allowed_origins" {
  description = "Web origins of the site whose upload form may call the upload API and post photos to the images bucket from the browser (CORS), such as [\"https://www.example.com\"]. Each is https://, a lowercase host name and an optional port, with no path or trailing slash; list every domain the form is served from, the Amplify default domain included if you use it. Set in terraform.tfvars."
  type        = list(string)

  validation {
    condition     = length(var.allowed_origins) > 0 && alltrue([for origin in var.allowed_origins : can(regex("^https://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+(:[0-9]{1,5})?$", origin))])
    error_message = "The allowed origins must be a non-empty list of https:// origins in lowercase, each without a path or trailing slash, such as https://www.example.com."
  }
}

variable "aws_region" {
  description = "AWS region to deploy into. Frontend currently points at eu-central-1."
  type        = string
  default     = "eu-central-1"

  validation {
    condition     = can(regex("^[a-z]+(-[a-z]+)+-[0-9]+$", var.aws_region))
    error_message = "The AWS region must be a region code, such as eu-central-1."
  }
}

variable "raw_prefix" {
  description = "S3 key prefix (a folder, ending in /) for raw card uploads. The IAM grants append * to it, so the trailing / keeps them inside this folder."
  type        = string
  default     = "images/raw/"

  validation {
    condition     = endswith(var.raw_prefix, "/") && !startswith(var.raw_prefix, "/")
    error_message = "The raw prefix must end with / and must not start with /."
  }
}

variable "max_upload_bytes" {
  description = "Max allowed upload size in bytes, enforced S3-side by the get-upload-url presigned POST (content-length-range). Default 26214400 = 25 MiB. The upload form mirrors this as a client-side UX check."
  type        = number
  default     = 26214400

  validation {
    condition     = var.max_upload_bytes >= 1 && floor(var.max_upload_bytes) == var.max_upload_bytes
    error_message = "The upload limit must be a whole number of bytes, at least 1."
  }
}

variable "finished_prefix" {
  description = "S3 key prefix (a folder, ending in /) where the image-processor writes finished card PNGs, which the review editor previews and the committer publishes."
  type        = string
  default     = "images/finished/"

  validation {
    condition     = endswith(var.finished_prefix, "/") && !startswith(var.finished_prefix, "/")
    error_message = "The finished prefix must end with / and must not start with /."
  }
}

variable "rejected_prefix" {
  description = "S3 key prefix (a folder, ending in /) where the card-detect gate quarantines uploads that don't look like a card. Dead-end (nothing triggers on it); add a lifecycle rule later to auto-expire it."
  type        = string
  default     = "images/rejected/"

  validation {
    condition     = endswith(var.rejected_prefix, "/") && !startswith(var.rejected_prefix, "/")
    error_message = "The rejected prefix must end with / and must not start with /."
  }
}

variable "static_cards_dir" {
  description = "Local folder that the 'new card' email's download command copies the finished card image into, relative to where you run the command, such as the static/cards folder of your site's checkout. The default, ., is the folder you run it in."
  type        = string
  default     = "."
}

variable "raw_download_dir" {
  description = "Local folder that the 'new card' email's download command copies the original photo into, relative to where you run the command. The default, ., is the folder you run it in."
  type        = string
  default     = "."
}

variable "corners_download_dir" {
  description = "Local folder that the 'new card' email's download command copies the corners overlay into, relative to where you run the command. The default, ., is the folder you run it in."
  type        = string
  default     = "."
}

variable "bedrock_llm" {
  description = "Bedrock inference-profile ID the image-processor uses for card-text extraction. Its geography prefix must match bedrock_region: an EU profile (eu.*) for an EU region such as eu-central-1."
  type        = string
  default     = "eu.amazon.nova-2-lite-v1:0"
}

variable "bedrock_region" {
  description = "AWS region where the Bedrock model/inference-profile is invoked. Typically equals aws_region, but kept separate because the Bedrock inference profile (bedrock_llm) is region-specific — a forker in us-east-1 needs both a us.* profile and this set to their region."
  type        = string
  default     = "eu-central-1"

  validation {
    condition     = can(regex("^[a-z]+(-[a-z]+)+-[0-9]+$", var.bedrock_region))
    error_message = "The Bedrock region must be a region code, such as eu-central-1."
  }
}

variable "github_publish_branch" {
  description = "Branch the committer Lambda commits published cards to (must already exist in the repo — the GitHub Contents API won't create it). FULL-AUTO: set this to your live branch, the one Amplify builds (amplify_branch_name), so approved cards deploy with no manual merge; the default, master, does that out of the box. PR-GATED alternative: set it to a separate staging branch you merge into the live branch yourself (the live branch can then keep 'require a PR' protection). Override in terraform.tfvars."
  type        = string
  default     = "master"
}

variable "amplify_branch_name" {
  description = "Amplify branch whose deploys trigger the SNS3 email (your live/deploy branch, normally equal to github_publish_branch). The EventBridge rule matches it exactly, so a mismatch such as main versus master silently drops the deploy email while the site still builds. Defaults to master. Override in terraform.tfvars."
  type        = string
  default     = "master"
}

variable "lambda_log_retention_days" {
  description = "Days CloudWatch Logs keeps the logs of each Lambda function before deleting them. Defaults to 90."
  type        = number
  default     = 90

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.lambda_log_retention_days)
    error_message = "The log retention must be a period CloudWatch Logs accepts: 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288 or 3653 days."
  }
}

variable "import_existing_log_groups" {
  description = "Set to true when the Lambda functions were deployed by a version of this stack that did not manage their log groups: Lambda then created the /aws/lambda/<function> log groups itself, and Terraform imports them instead of failing to create them. Leave false for a new deployment, where an import would fail because the log groups do not exist yet. Once they are in the state, either value is safe."
  type        = bool
  default     = false
}
