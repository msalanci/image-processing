# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# ECR repositories of the two container Lambdas, image-processor and card-detect, and their builds:
# on terraform apply, a change to a file hashed in a build's triggers rebuilds and pushes that arm64
# image, and lambda.tf deploys the pushed digest. The machine that runs apply needs Docker with
# buildx and the AWS CLI; the first image-processor build downloads its ~224 MB model and takes
# several minutes.

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

# Keep --provenance=false: BuildKit's default provenance attestation produces a manifest that Lambda
# refuses to deploy. Keep --load plus docker push: buildx --push is unreliable for large layers to
# ECR (repeated EOF errors).
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

# Same build flags as image_processor_build, for the same reasons. onnx_out/ is git-ignored: generate
# it in place with lambda/card-detect/build_onnx.py before the first apply, or filesha1 fails.
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
