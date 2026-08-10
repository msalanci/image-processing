# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# ECR repositories for the two container lambdas, built and pushed by terraform apply.
#
# Needs Docker with buildx, plus the AWS and Docker CLIs, on the machine running apply. 
# The first build downloads the segmentation weights and bakes them in, so it takes several minutes.

resource "aws_ecr_repository" "image_processor" {
  name                 = "buildercards-image-processor"
  image_tag_mutability = "MUTABLE"
  force_delete         = true # destroy works with images still in the repo

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Rebuilds when any of the hashed files change
resource "null_resource" "image_processor_build" {
  triggers = {
    dockerfile   = filesha1("${path.module}/lambda/image-processor/Dockerfile")
    app          = filesha1("${path.module}/lambda/image-processor/app.py")
    requirements = filesha1("${path.module}/lambda/image-processor/requirements.txt")
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      REGISTRY="${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
      aws ecr get-login-password --region ${var.aws_region} \
        | docker login --username AWS --password-stdin "$REGISTRY"
      # Build into the local image store, then push with the classic pusher, which handles
      # large layers to ECR far more reliably than buildx --push
      docker buildx build \
        --platform linux/arm64 \
        --provenance=false \
        --load \
        -t ${aws_ecr_repository.image_processor.repository_url}:latest \
        "${path.module}/lambda/image-processor"
      docker push ${aws_ecr_repository.image_processor.repository_url}:latest
    EOT
  }
}

# Reading the digest after the build
data "aws_ecr_image" "image_processor" {
  repository_name = aws_ecr_repository.image_processor.name
  image_tag       = "latest"
  depends_on      = [null_resource.image_processor_build]
}

resource "aws_ecr_repository" "card_detect" {
  name                 = "buildercards-card-detect"
  image_tag_mutability = "MUTABLE"
  force_delete         = true # destroy works with images still in the repo

  image_scanning_configuration {
    scan_on_push = true
  }
}

# The model artifacts sit in the build context already, so the Dockerfile copies 
# the frozen, parity-checked bytes instead of re-exporting them
resource "null_resource" "card_detect_build" {
  triggers = {
    dockerfile    = filesha1("${path.module}/lambda/card-detect/Dockerfile")
    index         = filesha1("${path.module}/lambda/card-detect/index.py")
    requirements  = filesha1("${path.module}/lambda/card-detect/requirements.txt")
    model_weights = filesha1("${path.module}/lambda/card-detect/onnx_out/visual.onnx.data")
    model_text    = filesha1("${path.module}/lambda/card-detect/onnx_out/text.npz")
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      REGISTRY="${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
      aws ecr get-login-password --region ${var.aws_region} \
        | docker login --username AWS --password-stdin "$REGISTRY"
      docker buildx build \
        --platform linux/arm64 \
        --provenance=false \
        --load \
        -t ${aws_ecr_repository.card_detect.repository_url}:latest \
        "${path.module}/lambda/card-detect"
      docker push ${aws_ecr_repository.card_detect.repository_url}:latest
    EOT
  }
}

data "aws_ecr_image" "card_detect" {
  repository_name = aws_ecr_repository.card_detect.name
  image_tag       = "latest"
  depends_on      = [null_resource.card_detect_build]
}
