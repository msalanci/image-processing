# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
#
# Container image for the image-processor Lambda, which card-detect invokes for each photo that may
# be a card: it cuts the card out with rembg (BiRefNet on onnxruntime, CPU), straightens it with
# OpenCV, has Bedrock draft its text, and stores the result for the maintainer's review.
# Inputs: requirements.txt, app.py and the REMBG_MODEL build argument. The model is downloaded
# into the image at build time, so a cold start never fetches it.
# Lambda needs an arm64 image without attestations:
#   docker buildx build --platform linux/arm64 --provenance=false -t buildercards-image-processor .

FROM public.ecr.aws/lambda/python:3.14@sha256:fc4e15a5e09e2c9b6184810ea0fdc87afe1c1c734c98cbe27d739a35c3f15e7e

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir --only-binary=:all: -r "${LAMBDA_TASK_ROOT}/requirements.txt"

ARG REMBG_MODEL=birefnet-general-lite

# Lambda can only write to /tmp, where numba keeps its cache. app.py loads REMBG_MODEL from U2NET_HOME.
ENV REMBG_MODEL=${REMBG_MODEL} \
    U2NET_HOME=/opt/models \
    NUMBA_CACHE_DIR=/tmp

# rembg's downloader writes the model with mode 0600, which Lambda's non-root user cannot read.
RUN python -c "import os; from rembg import new_session; new_session(os.environ['REMBG_MODEL'])" \
    && chmod -R a+rX "${U2NET_HOME}"

COPY app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
