# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# Container image for the card-detect Lambda: scores each uploaded photo with a CLIP ViT-B/32
# image encoder on CPU (onnxruntime) and passes likely cards on to the image-processor.
# Inputs: requirements.txt, index.py and onnx_out/ (visual.onnx, visual.onnx.data, text.npz),
# which build_onnx.py in this folder generates. The model is baked in, so a cold start downloads nothing.
# Lambda needs an arm64 image without attestations:
#   docker buildx build --platform linux/arm64 --provenance=false -t buildercards-card-detect .

FROM public.ecr.aws/lambda/python:3.14@sha256:fc4e15a5e09e2c9b6184810ea0fdc87afe1c1c734c98cbe27d739a35c3f15e7e

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir --only-binary=:all: -r "${LAMBDA_TASK_ROOT}/requirements.txt"

ENV MODEL_DIR=/opt/model
COPY onnx_out/visual.onnx onnx_out/visual.onnx.data onnx_out/text.npz ${MODEL_DIR}/
# Lambda runs the function as a non-root user: the model must be readable whatever the builder's umask.
RUN chmod -R a+rX "${MODEL_DIR}"

COPY index.py ${LAMBDA_TASK_ROOT}/

CMD ["index.handler"]
