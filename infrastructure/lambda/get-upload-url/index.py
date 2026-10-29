# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci

"""Public upload endpoint of the card pipeline: hands the upload form a pre-signed S3 POST for one photo.

API Gateway HTTP API invokes the handler on POST /get-upload-url (payload format 2.0), with no
authentication. The handler validates and sanitises the request, then signs a POST that lets the browser
send one card photo straight to the images bucket under RAW_PREFIX, where the card-detect gate picks it
up. The photo never passes through this function. It returns the POST url and form fields, or a 400 with
a JSON error; it writes nothing itself.

Environment variables:
    AWS_REGION: Region for the S3 client, set by the Lambda runtime.
    BUCKET_NAME: The images bucket that receives the upload.
    RAW_PREFIX: Key prefix for raw uploads, for example "images/raw/".
    MAX_UPLOAD_BYTES: Optional upload size cap in bytes, 25 MiB when unset. S3 enforces it through the
        signed POST policy, not this code.

An accepted upload can lead to a 10 GB container run and a Bedrock call, so every field is checked before
a URL is signed. These checks are defence in depth alongside the API stage throttle and the
image-processor's reserved concurrency.

Limits:
    ALLOWED_CONTENT_TYPES: Browser image types only, so a request that declares a video, archive or
        other non-image type is rejected while real photos are not. Only the declared type is checked:
        the signed policy pins the Content-Type field, but S3 does not inspect the uploaded bytes.
        PNG, HEIC and WebP are accepted besides JPEG because phone photos often come in those
        formats, and BMP and TIFF because Pillow (in the gate) and OpenCV (in the image-processor)
        decode both natively. AVIF is left out on purpose: it needs extra native decode libraries in
        both container Lambdas.
    MAX_EVENT_NAME_LEN: Longest event name accepted, counted before it is sanitised into the key.
    MAX_UPLOADER_LEN: Longest uploader name accepted; it is stored as S3 object metadata.
    MIN_YEAR: Earliest card year accepted, because Builder Cards did not exist before 2020. The latest
        is the current year.
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

ALLOWED_CONTENT_TYPES = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif",
    "image/bmp", "image/tiff",
}
MAX_EVENT_NAME_LEN = 100
MAX_UPLOADER_LEN = 60
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
MIN_YEAR = 2020


def _resp(status, body):
    """
    Builds an API Gateway HTTP API response with JSON body and CORS headers.

    Every return path in this Lambda goes through here, so the content type and the CORS header are
    set in exactly one place. The permissive origin lets the Amplify-hosted Hugo frontend call the
    endpoint from the browser and also read the JSON error body on a 400 — without it, a validation
    failure would surface to the user as an opaque network error.

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
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def handler(event, context):
    """
    Lambda entry point: validates an upload request and returns a pre-signed POST for S3.

    A request is accepted only when contentType, eventName and uploader are each a string, null or
    missing; contentType, trimmed and lower-cased, is in ALLOWED_CONTENT_TYPES (image/jpeg when missing,
    null or empty); year is four ASCII digits between MIN_YEAR and the current UTC year; and the trimmed
    eventName and uploader are at most MAX_EVENT_NAME_LEN and MAX_UPLOADER_LEN characters. The upper
    year bound is read from the clock on every request, so it advances on 1 January without a redeploy
    and no card can be dated in the future. A body that json.loads cannot read counts as an empty
    object, so it is rejected for its missing year instead of raising. Besides invalid JSON, that
    covers a number longer than Python's 4300-digit limit, which raises ValueError rather than
    JSONDecodeError, and nesting too deep for the stack, which raises RecursionError.

    The object key is RAW_PREFIX followed by "<event>_<year>_<uuid>.jpg". The event part keeps only ASCII
    letters, digits and hyphens, at most 60 characters, and is "card" when nothing is left, so it never
    holds the "_" that the image-processor's parse_meta() splits the key on. The key always ends in .jpg
    whatever the real format, because the S3 trigger filters on that suffix and both downstream decoders
    read the bytes, not the name.

    It signs a POST rather than a PUT because only a POST policy can cap the upload size: the
    content-length-range condition makes S3 reject an empty body or one over MAX_UPLOAD_BYTES, and the
    browser must send the returned fields unchanged. The uploader travels as the signed form field
    x-amz-meta-uploader; S3 stores it as the object metadata key "uploader", which the image-processor
    reads, so the field name must stay as it is.

    Args:
        event: API Gateway HTTP API payload v2.0 event. Only its "body" is read: a JSON object such as
            {"contentType": "image/jpeg", "eventName": "re:Invent 2024", "year": "2024", "uploader": "..."}.
        context: Lambda context object, unused.

    Returns:
        A 200 response with uploadUrl, the signed form fields, objectKey, maxUploadBytes and the echoed
        metadata (eventName, year, uploader); the POST is valid for 5 minutes. Otherwise a 400 response
        whose "error" names the rejected field, or says that the body is not a JSON object.
    """

    body = event.get("body") or "{}"
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        data = {}
    if not isinstance(data, dict):
        return _resp(400, {"error": "body must be a JSON object"})
    for field in ("contentType", "eventName", "uploader"):
        if data.get(field) is not None and not isinstance(data[field], str):
            return _resp(400, {"error": f"{field} must be a string"})

    content_type = (data.get("contentType") or "image/jpeg").strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        return _resp(400, {"error": "contentType must be an image type (jpeg/png/webp/heic/bmp/tiff)"})

    year = str(data.get("year") or "").strip()
    if not re.fullmatch(r"[0-9]{4}", year):
        return _resp(400, {"error": "year must be 4 digits, e.g. 2024"})
    current_year = datetime.now(timezone.utc).year
    if not (MIN_YEAR <= int(year) <= current_year):
        return _resp(400, {"error": f"year must be between {MIN_YEAR} and {current_year}"})

    event_name = (data.get("eventName") or "").strip()
    if len(event_name) > MAX_EVENT_NAME_LEN:
        return _resp(400, {"error": f"eventName too long (max {MAX_EVENT_NAME_LEN} chars)"})

    uploader = (data.get("uploader") or "").strip()
    if len(uploader) > MAX_UPLOADER_LEN:
        return _resp(400, {"error": f"uploader too long (max {MAX_UPLOADER_LEN} chars)"})

    safe_event = re.sub(r"[^A-Za-z0-9-]+", "-", event_name).strip("-")[:60] or "card"
    object_key = f"{RAW_PREFIX}{safe_event}_{year}_{uuid.uuid4()}.jpg"

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
