# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci

"""Gate that decides whether an uploaded image is an AWS Builder Card.

An S3 event for a new .jpg under images/raw/ invokes this Lambda. It scores the image with a CLIP
ViT-B/32 image encoder on CPU through onnxruntime, against label vectors that build_onnx.py computes
at build time, so no PyTorch runs here. Likely and borderline AWS cards are forwarded to the
image-processor Lambda; everything else is moved to images/rejected/ and reported by email. The gate
is a function of its own because the image-processor's memory is already close to its limit.

The default DROP_THRESHOLD of 0.20 sits in the measured gap between real AWS cards (0.31 to 0.87)
and other cards or non-cards (at most 0.016). Scores between the two thresholds are still forwarded,
so the gate errs toward processing. The encoder and the label vectors load once per container and
warm invocations reuse them. HEIC and HEIF photos decode through pillow-heif, whose wheel bundles
libheif; if it cannot be loaded, a warning is logged and the other formats still decode.

Functions:
    handler: Entry point; scores each record and forwards or quarantines it.
    p_card: Returns the AWS-card and any-card probabilities for one image.
    preprocess_np: Resizes and normalises an image into the encoder's input tensor.

Environment (set by Terraform unless noted; the defaults in the code only matter for local runs):
    RAW_PREFIX                      (images/raw/) input prefix the function reads
    REJECTED_PREFIX                 (images/rejected/) quarantine prefix, a dead end
    IMAGE_PROCESSOR_FUNCTION_NAME   the processor lambda the function forwards to
    REJECT_TOPIC_ARN                SNS topic for reject notifications
    REJECTED_TAB_URL                the review editor's Rejected tab, linked from the reject email
    KEEP_THRESHOLD / DROP_THRESHOLD the two score cutoffs (floats, defaults 0.50 and 0.20)
    MODEL_DIR                       (/opt/model) where visual.onnx, visual.onnx.data and
                                    text.npz live; set by the Dockerfile, not by Terraform
"""

import os
import io
import json
import logging
import urllib.parse

import boto3
import numpy as np
import onnxruntime as ort
from PIL import Image

logger = logging.getLogger()
logger.setLevel(logging.INFO)

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception as _heif_err:  # pragma: no cover
    logger.warning("pillow-heif unavailable; HEIC/HEIF uploads will not decode: %s", _heif_err)

RAW_PREFIX = os.environ.get("RAW_PREFIX", "images/raw/")
REJECTED_PREFIX = os.environ.get("REJECTED_PREFIX", "images/rejected/")
IMAGE_PROCESSOR_FUNCTION_NAME = os.environ.get("IMAGE_PROCESSOR_FUNCTION_NAME", "")
REJECT_TOPIC_ARN = os.environ.get("REJECT_TOPIC_ARN", "")
REJECTED_TAB_URL = os.environ.get("REJECTED_TAB_URL", "")

# S3 lowercases user metadata keys and boto3 drops their x-amz-meta- prefix;
# the review editor writes this same key.
FORCE_PROCESS_META = "force-process"

KEEP_THRESHOLD = float(os.environ.get("KEEP_THRESHOLD", "0.50"))
DROP_THRESHOLD = float(os.environ.get("DROP_THRESHOLD", "0.20"))

MODEL_DIR = os.environ.get("MODEL_DIR", "/opt/model")
VISUAL_ONNX = os.path.join(MODEL_DIR, "visual.onnx")
TEXT_NPZ = os.path.join(MODEL_DIR, "text.npz")

# Must match the open_clip model that build_onnx.py exports, or the embeddings drift
# from the PyTorch reference.
IMAGE_SIZE = 224
CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

s3 = boto3.client("s3")
sns = boto3.client("sns")
lambda_client = boto3.client("lambda")

_SESSION = ort.InferenceSession(VISUAL_ONNX, providers=["CPUExecutionProvider"])
_INPUT_NAME = _SESSION.get_inputs()[0].name
_TEXT = np.load(TEXT_NPZ, allow_pickle=True)
_TEXT_EMBEDS = _TEXT["embeds"].astype(np.float32)
_LOGIT_SCALE = float(_TEXT["logit_scale"])
_CARD_COUNT = int(_TEXT["card_count"])
_AWS_COUNT = int(_TEXT["aws_count"]) if "aws_count" in _TEXT else _CARD_COUNT


def preprocess_np(pil_img, size=IMAGE_SIZE):
    """Turn an image into the encoder's input tensor, as open_clip's evaluation transform does.

    It replicates that transform bit-exactly without torchvision: a bicubic resize of the shorter
    side, a centre crop and the CLIP mean/std normalisation. The long edge is truncated with int(),
    not rounded, because that is what torchvision's Resize does; rounding makes the ONNX embeddings
    drift from the PyTorch reference.

    Args:
        pil_img: Decoded input image as a PIL Image, any mode.
        size: Target square resolution in pixels, matching the ViT-B/32 input size.

    Returns:
        Contiguous float32 array of shape (1, 3, size, size), normalised and ready for the encoder.
    """

    img = pil_img.convert("RGB")
    w, h = img.size
    if w <= h:
        nw, nh = size, int(h * size / w)
    else:
        nh, nw = size, int(w * size / h)
    img = img.resize((nw, nh), Image.BICUBIC)

    left, top = (nw - size) // 2, (nh - size) // 2
    img = img.crop((left, top, left + size, top + size))

    x = np.asarray(img).astype(np.float32) / 255.0
    x = (x - CLIP_MEAN) / CLIP_STD

    x = np.transpose(x, (2, 0, 1))[None, ...]
    return np.ascontiguousarray(x, dtype=np.float32)


def p_card(raw_bytes):
    """Score an image against the label set and return its AWS-card and any-card probabilities.

    The image embedding is compared with every label vector in text.npz (cosine similarity times the
    model's logit scale) and one softmax runs over all labels. Because the labels share that one
    softmax, a competitor label pulls probability away from the AWS labels: that is what keeps
    p_aws low for other card types. The softmax subtracts the row maximum first, so exp() cannot
    overflow. build_onnx.py stores the labels in the order AWS, competitor, non-card, with the
    group sizes as aws_count and card_count; a text.npz without aws_count makes p_aws equal
    p_any_card.

    Args:
        raw_bytes: Encoded input image as downloaded from S3 (jpg/png/webp/bmp/tiff/heic).

    Returns:
        Tuple of (p_aws, p_any_card):
            p_aws      Softmax mass over the AWS-specific labels — the AWS-ness score, and the
                       value the gate actually decides on. LOW for non-AWS cards, because
                       Pokemon/poker/sports land on their own competitor labels instead.
            p_any_card Mass over the AWS and competitor labels together, i.e. "is this any kind
                       of card at all?". Never gates; it only labels WHY something was rejected.

    Raises:
        PIL.UnidentifiedImageError: When the bytes are not an image format Pillow can decode.
    """

    img = Image.open(io.BytesIO(raw_bytes))
    x = preprocess_np(img)

    emb = _SESSION.run(None, {_INPUT_NAME: x})[0]
    emb = emb / np.linalg.norm(emb, axis=-1, keepdims=True)

    logits = _LOGIT_SCALE * (emb @ _TEXT_EMBEDS.T)

    z = logits - logits.max(axis=-1, keepdims=True)
    e = np.exp(z)
    probs = (e / e.sum(axis=-1, keepdims=True))[0]

    p_aws = float(probs[:_AWS_COUNT].sum())
    p_any_card = float(probs[:_CARD_COUNT].sum())
    return p_aws, p_any_card


def _forward_to_processor(record):
    """Hand an accepted upload to the image-processor Lambda with an asynchronous invoke.

    The record goes out unmodified, wrapped as a one-record S3 event, which is the shape the
    processor's handler reads. The invoke is asynchronous, so the gate does not wait out the
    60-180 second processing run, and Lambda's async retries apply as they would for S3 delivery.
    A missing function name is logged rather than raised, so one misconfigured environment
    variable cannot turn every upload into a failed invocation.

    Args:
        record: A single S3 event record, forwarded unmodified so the processor cannot tell it
            was not invoked by S3 directly.

    Returns:
        None. Sends the asynchronous invoke, or logs an error when IMAGE_PROCESSOR_FUNCTION_NAME
        is empty.

    Raises:
        botocore.exceptions.ClientError: When Lambda refuses the invoke.
    """

    if not IMAGE_PROCESSOR_FUNCTION_NAME:
        logger.error("[CardDetect] IMAGE_PROCESSOR_FUNCTION_NAME not set — cannot forward")
        return

    lambda_client.invoke(
        FunctionName=IMAGE_PROCESSOR_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps({"Records": [record]}).encode("utf-8"),
    )


def _quarantine(bucket, key):
    """Move a rejected upload from the raw prefix to the rejected prefix, keeping its file name.

    S3 has no move, so the object is copied and the original deleted. Nothing triggers on the
    rejected prefix, so the upload is kept for inspection or for copying back to be processed after
    all, and the versioned bucket keeps a recoverable version of the deleted original.

    Args:
        bucket: Name of the S3 bucket holding the upload.
        key: Full key of the rejected object, under the raw prefix.

    Returns:
        The new key under the rejected prefix, used in the notification email.

    Raises:
        botocore.exceptions.ClientError: When S3 refuses the copy or the delete.
    """

    rejected_key = REJECTED_PREFIX + key[len(RAW_PREFIX):]

    s3.copy_object(
        Bucket=bucket,
        Key=rejected_key,
        CopySource={"Bucket": bucket, "Key": key},
    )
    s3.delete_object(Bucket=bucket, Key=key)
    return rejected_key


def _publish_reject(bucket, key, rejected_key, p):
    """Email the maintainer that an upload was auto-rejected, with its score and where it was moved.

    It publishes to its own SNS topic rather than the pipeline's review topic, because a rejected
    upload has no cardId, no finished image and no review page. The message carries the score, the
    cutoff it missed, both S3 locations and the link to the review editor's Rejected tab. Recovery
    goes through that tab's "Put to process" button rather than a plain move back to the raw prefix,
    because only the button adds the force-process marker (a moved-back image is scored the same way
    and rejected again) while keeping the upload's metadata. Sending is best effort, so a broken
    notification cannot fail an otherwise correct rejection.

    SNS refuses a Subject of 100 characters or more, and a file name can be over 100 characters
    long (an event name of up to 60, the year and a UUID), so the file name in the Subject is cut
    to keep it at 99; the full name is in the message.

    Args:
        bucket: Name of the S3 bucket holding the upload.
        key: Original key of the rejected object, before it was quarantined.
        rejected_key: New key under the quarantine prefix, as returned by _quarantine().
        p: The AWS-card probability that fell below the drop cutoff.

    Returns:
        None. Publishes one message to REJECT_TOPIC_ARN; skips it when the topic is not set, and
        logs a failed publish instead of raising.
    """

    if not REJECT_TOPIC_ARN:
        logger.info("[CardDetect] REJECT_TOPIC_ARN not set, skipping reject email")
        return

    name = key.split("/")[-1]
    message = f"""A Builder Card upload was auto-rejected by the card-detect gate.

Its AWS-card score was below the cutoff — it's either a NON-AWS card (Pokemon /
poker / sports / TCG / etc.) or not a card at all — so it was NOT processed: no
background removal, no AI draft, no DynamoDB item, no review page.

=== DETAILS ===
File: {name}
P(AWS card): {p:.4f}  (drop cutoff: < {DROP_THRESHOLD:.2f})
Original (now moved): s3://{bucket}/{key}
Quarantined to: s3://{bucket}/{rejected_key}

The image was NOT deleted — it was moved to the images/rejected/ prefix (the
bucket is versioned, so it is recoverable). If this was a real card the gate got
wrong, open the review editor's Rejected tab and press "Put to process" next to
it. That sends it back through the pipeline with its event, year and uploader,
and marks it so the gate lets it through:

  {REJECTED_TAB_URL or "<review editor>/review?status=rejected"}
"""
    score = f" (P(AWS)={p:.2f})"
    subject = f"Rejected upload: {name}"[:99 - len(score)] + score
    try:
        sns.publish(
            TopicArn=REJECT_TOPIC_ARN,
            Subject=subject,
            Message=message,
        )
        logger.info("[CardDetect] reject email sent for %s", key)
    except Exception as e:  # noqa: BLE001 - non-fatal
        logger.exception("[CardDetect] failed to send reject email: %s", e)


def handler(event, context):
    """Lambda entry point: score every uploaded image and forward it or quarantine it.

    Keys arrive URL-encoded in S3 event notifications, so they are decoded first. Records without a
    bucket or key are skipped, and so are .keep placeholders, corner-overlay images and keys outside
    the raw prefix, with the same guards as the image-processor, although the S3 notification
    already filters on the raw prefix and the .jpg suffix.

    An upload carrying the force-process marker, which the review editor's "put to process" sets on
    a rejected image it sends back, is forwarded without scoring, because the identical image would
    score the same and be rejected again; the marker is read before the body, so the model does not
    run. Every other upload is forwarded when p_aws reaches DROP_THRESHOLD and quarantined with a
    reject email below it; KEEP_THRESHOLD and p_any_card only choose the verdict label. A record
    that raises is logged and recorded as an error, and the remaining records continue.

    When any record failed, the function raises after the loop. S3 invokes it asynchronously, and
    Lambda retries an asynchronous event only when the function fails, so returning normally would
    leave a temporarily failed upload in the raw prefix for good. A retry is safe: the upload is still
    under its raw key until it is forwarded or quarantined, and S3 sends one record per event.

    Args:
        event: S3 event notification; only its "Records" list is read.
        context: Lambda context object, unused.

    Returns:
        HTTP-shaped dict with status 200, whose JSON body holds one result per record that was not
        skipped: the key, the verdict ("forced", "aws-card", "aws-unsure", "non-aws-card" or
        "not-a-card") and the action ("forwarded" or "rejected"). Scored records also carry p_aws
        and, under the key "p_card", p_any_card. Every forwarded or rejected record is also logged as
        one JSON line. An empty results list means no record was handled.

    Raises:
        RuntimeError: One or more records failed; the message lists their keys.
    """

    logger.info("[CardDetect] keep>=%.2f drop<%.2f", KEEP_THRESHOLD, DROP_THRESHOLD)
    results = []

    for record in event.get("Records", []):
        s3_info = record.get("s3", {})
        bucket = s3_info.get("bucket", {}).get("name")
        key = s3_info.get("object", {}).get("key")
        if not bucket or not key:
            logger.info("[CardDetect] missing bucket/key, skipping")
            continue

        key = urllib.parse.unquote_plus(key)

        if key.endswith(".keep"):
            logger.info("[CardDetect] skipping .keep placeholder")
            continue
        if key.endswith("__corners.png"):
            logger.info("[CardDetect] skipping corners-overlay image (not a card)")
            continue
        if not key.startswith(RAW_PREFIX):
            logger.info("[CardDetect] key not under RAW_PREFIX=%s, skipping", RAW_PREFIX)
            continue

        try:
            obj = s3.get_object(Bucket=bucket, Key=key)

            if obj.get("Metadata", {}).get(FORCE_PROCESS_META):
                _forward_to_processor(record)
                logger.info(json.dumps({"key": key, "verdict": "forced", "action": "forwarded"}))
                results.append({"key": key, "verdict": "forced", "action": "forwarded"})
                continue

            raw = obj["Body"].read()
            p_aws, p_any = p_card(raw)

            if p_aws >= DROP_THRESHOLD:
                verdict = "aws-card" if p_aws >= KEEP_THRESHOLD else "aws-unsure"
                _forward_to_processor(record)
                action = "forwarded"
            else:
                verdict = "non-aws-card" if p_any >= DROP_THRESHOLD else "not-a-card"
                rejected_key = _quarantine(bucket, key)
                _publish_reject(bucket, key, rejected_key, p_aws)
                action = "rejected"

            logger.info(json.dumps({
                "key": key, "p_aws": round(p_aws, 4), "p_card": round(p_any, 4),
                "verdict": verdict, "action": action,
            }))
            results.append({
                "key": key, "p_aws": round(p_aws, 4), "p_card": round(p_any, 4),
                "verdict": verdict, "action": action,
            })
        except Exception as e:  # noqa: BLE001 - log and continue to next record
            logger.exception("[CardDetect] failed on %s: %s", key, e)
            results.append({"key": key, "status": "error", "error": str(e)})

    failed = [r["key"] for r in results if r.get("status") == "error"]
    if failed:
        raise RuntimeError(f"{len(failed)} record(s) failed, raising so Lambda retries the event: {failed}")
    return {"statusCode": 200, "body": json.dumps({"results": results})}
