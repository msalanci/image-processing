# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Michal Salanci
 
"""Hands the upload form a pre-signed POST, so a photo goes straight to S3.

This is the only public, unauthenticated endpoint in the stack, and it gates the expensive path, 
so every field is checked before a URL is signed. 
The photo itself never passes through this function, and the signed policy caps its size.

Functions:
    handler: Entry point; validates the request and returns the pre-signed POST.

Environment (set by Terraform):
    BUCKET_NAME         bucket the uploads land in
    RAW_PREFIX          key prefix for new uploads
    MAX_UPLOAD_BYTES    size cap enforced by S3 (default 25 MiB)
    AWS_REGION          provided automatically by the Lambda runtime
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone

import boto3

s3 = boto3.client("s3", region_name=os.environ["AWS_REGION"])
BUCKET = os.environ["BUCKET_NAME"]
RAW_PREFIX = os.environ["RAW_PREFIX"]

# Only browser image types, which blocks video and archives without rejecting real photos
ALLOWED_CONTENT_TYPES = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif",
    "image/bmp", "image/tiff",
}
MAX_EVENT_NAME_LEN = 100  # raw eventName cap, before it is sanitised into the key
MAX_UPLOADER_LEN = 60     # uploader cap, since it is stored as S3 object metadata
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))  # 25 MiB
MIN_YEAR = 2020           # Builder Cards did not exist before 2020; the ceiling is the clock


def _resp(status, body):
    """
    Builds an API Gateway response with a JSON body and CORS headers.

    Every return path goes through here, so the headers are set in one place. 
    The open origin lets the website read the JSON error body on a 400 instead of seeing a network error.

    Args:
        status: HTTP status code to return.
        body: JSON-serialisable dict for the response body.

    Returns:
        API Gateway response dict with statusCode, headers and a JSON-encoded body.
    """
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            
            # Lets the website call this endpoint and read the error body on a 400
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def handler(event, context):
    """
    Lambda entry point: validates an upload request and returns a pre-signed POST for S3.

    The key always ends in .jpg whatever the real format, because the S3 trigger filters on that suffix 
    and the decoders downstream read the bytes, not the name. 
    It signs a POST rather than a PUT, because only a POST policy can cap the upload size.

    Args:
        event: API Gateway HTTP API payload v2.0 event; only its JSON "body" is read, carrying contentType, eventName, year and uploader.
        context: Lambda context object, unused.

    Returns:
        200 response with uploadUrl, the signed form fields, objectKey, maxUploadBytes and the echoed metadata; 
        or a 400 response with an "error" describing the rejected field.
    """

    # Parse the body defensively, so a malformed payload cannot raise
    body = event.get("body") or "{}"
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        return _resp(400, {"error": "body must be a JSON object"})

    # Content type must be on the image allowlist, which blocks video and archive payloads
    content_type = (data.get("contentType") or "image/jpeg").strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        return _resp(400, {"error": "contentType must be an image type (jpeg/png/webp/heic/bmp/tiff)"})

    # The ceiling is read from the clock on every request, so no card can be dated in the future
    year = str(data.get("year") or "").strip()
    if not re.fullmatch(r"\d{4}", year):
        return _resp(400, {"error": "year must be 4 digits, e.g. 2024"})
    current_year = datetime.now(timezone.utc).year
    if not (MIN_YEAR <= int(year) <= current_year):
        return _resp(400, {"error": f"year must be between {MIN_YEAR} and {current_year}"})

    # Length-cap the two free-text fields before they reach the key and the metadata
    event_name = (data.get("eventName") or "").strip()
    if len(event_name) > MAX_EVENT_NAME_LEN:
        return _resp(400, {"error": f"eventName too long (max {MAX_EVENT_NAME_LEN} chars)"})

    uploader = (data.get("uploader") or "").strip()
    if len(uploader) > MAX_UPLOADER_LEN:
        return _resp(400, {"error": f"uploader too long (max {MAX_UPLOADER_LEN} chars)"})

    # Strip the event name down to letters, digits and hyphens
    safe_event = re.sub(r"[^A-Za-z0-9-]+", "-", event_name).strip("-")[:60] or "card"
    object_key = f"{RAW_PREFIX}{safe_event}_{year}_{uuid.uuid4()}.jpg"

    # policy S3 enforces at upload time
    post = s3.generate_presigned_post(
        Bucket=BUCKET,
        Key=object_key,
        Fields={
            "Content-Type": content_type,
            "x-amz-meta-uploader": uploader,
        },
        Conditions=[
            {"Content-Type": content_type},
            {"x-amz-meta-uploader": uploader},
            ["content-length-range", 1, MAX_UPLOAD_BYTES],
        ],
        ExpiresIn=300,
    )

    return _resp(200, {
        "uploadUrl": post["url"],
        "fields": post["fields"],
        "objectKey": object_key,
        "maxUploadBytes": MAX_UPLOAD_BYTES,
        "metadata": {"eventName": event_name, "year": year, "uploader": uploader},
    })
