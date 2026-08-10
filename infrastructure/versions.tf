# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# Terraform and provider version constraints

terraform {
  required_version = ">= 1.15.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.51"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.3"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      terraform = "true"
      project   = "buildercards"
    }
  }
}
