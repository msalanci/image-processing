# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# ECR repositories of the two container Lambdas, image-processor and card-detect, and their builds:
# on terraform apply, a change to a file hashed in a build's triggers rebuilds and pushes that arm64
# image, and lambda.tf deploys the pushed digest of the latest tag. Each repository keeps its 3 newest
# untagged images. The machine that runs apply needs Docker with buildx and the AWS CLI; the first
# image-processor build downloads its ~224 MB model and takes several minutes.

locals {
  ecr_untagged_images_policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images beyond the 3 newest"
        selection = {
          tagStatus   = "untagged"
          countType   = "imageCountMoreThan"
          countNumber = 3
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

# depends_on makes each digest read wait for its build, so the Lambda gets the image just pushed.
data "aws_ecr_image" "image_processor" {
  repository_name = aws_ecr_repository.image_processor.name
  image_tag       = "latest"
  depends_on      = [null_resource.image_processor_build]
}

data "aws_ecr_image" "card_detect" {
  repository_name = aws_ecr_repository.card_detect.name
  image_tag       = "latest"
  depends_on      = [null_resource.card_detect_build]
}

resource "aws_ecr_repository" "image_processor" {
  name                 = "buildercards-image-processor"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "image_processor" {
  repository = aws_ecr_repository.image_processor.name
  policy     = local.ecr_untagged_images_policy
}

# Keep --provenance=false: BuildKit's default provenance attestation produces a manifest that Lambda
# refuses to deploy. Keep --load plus docker push: buildx --push is unreliable for large layers to
# ECR (repeated EOF errors).
resource "null_resource" "image_processor_build" {
  triggers = {
    dockerfile   = filesha1("${path.module}/lambda/image-processor/Dockerfile")
    dockerignore = filesha1("${path.module}/lambda/image-processor/.dockerignore")
    app          = filesha1("${path.module}/lambda/image-processor/app.py")
    requirements = filesha1("${path.module}/lambda/image-processor/requirements.txt")
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      REGISTRY="${split("/", aws_ecr_repository.image_processor.repository_url)[0]}"
      aws ecr get-login-password --region ${var.aws_region} \
        | docker login --username AWS --password-stdin "$REGISTRY"
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

resource "aws_ecr_repository" "card_detect" {
  name                 = "buildercards-card-detect"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "card_detect" {
  repository = aws_ecr_repository.card_detect.name
  policy     = local.ecr_untagged_images_policy
}

# Same build flags as image_processor_build, for the same reasons. onnx_out/ is git-ignored: generate
# it in place with lambda/card-detect/build_onnx.py before the first apply, or filesha1 fails.
# It runs after image_processor_build: both builds docker login to the same registry, and two logins
# at the same moment can fail on macOS with "The specified item already exists in the keychain".
resource "null_resource" "card_detect_build" {
  triggers = {
    dockerfile    = filesha1("${path.module}/lambda/card-detect/Dockerfile")
    dockerignore  = filesha1("${path.module}/lambda/card-detect/.dockerignore")
    index         = filesha1("${path.module}/lambda/card-detect/index.py")
    requirements  = filesha1("${path.module}/lambda/card-detect/requirements.txt")
    model_graph   = filesha1("${path.module}/lambda/card-detect/onnx_out/visual.onnx")
    model_weights = filesha1("${path.module}/lambda/card-detect/onnx_out/visual.onnx.data")
    model_text    = filesha1("${path.module}/lambda/card-detect/onnx_out/text.npz")
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      REGISTRY="${split("/", aws_ecr_repository.card_detect.repository_url)[0]}"
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

  depends_on = [null_resource.image_processor_build]
}
