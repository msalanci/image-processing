# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Michal Salanci
#
# CPU container image for the card-cleanup Lambda (rembg/BiRefNet + OpenCV).
# Build for arm64 (Graviton).
#
# Build (on the M4 mac, native arm64):
#   docker build --platform linux/arm64 -t buildercards-image-processor .
#
# The BiRefNet-lite weights (~224 MB) are downloaded at BUILD time into /opt/models socold starts do not fetch them. 
# U2NET_HOME tells rembg where to find them.

FROM public.ecr.aws/lambda/python:3.14

# onnxruntime needs libgomp (OpenMP runtime); not in the base image by default.
RUN dnf install -y libgomp && dnf clean all

# Writable/baked paths. /opt is read-only at runtime but writable at build time.
ENV U2NET_HOME=/opt/models \
    NUMBA_CACHE_DIR=/tmp \
    MPLCONFIGDIR=/tmp

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download BiRefNet weights into U2NET_HOME so they ship inside the image.
RUN python -c "from rembg import new_session; new_session('birefnet-general-lite')" \
    && chmod -R a+rX /opt/models

COPY app.py ${LAMBDA_TASK_ROOT}/

# Lambda handler entrypoint: <module>.<function>
CMD ["app.handler"]
