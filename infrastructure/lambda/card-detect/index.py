# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci

"""Gate that decides whether an uploaded image is an AWS Builder Card.

An S3 event on images/raw/ invokes this lambda, which scores the image with a CLIP ViT-B/32 encoder running on CPU through onnxruntime. 
Likely cards are forwarded to the image-processor lambda for the full cleanup pipeline; 
everything else is moved to images/rejected/ and reported by email. 
The function errs toward forwarding, so a borderline score is still processed.

Functions:
    handler: Entry point; scores each record and forwards or quarantines it.
    p_card: Returns the AWS-card and any-card probabilities for one image.
    preprocess_np: Resizes and normalises an image into the encoder's input tensor.

Environment (set by Terraform):
    RAW_PREFIX                      (images/raw/) input prefix the function reads
    REJECTED_PREFIX                 (images/rejected/) quarantine prefix, a dead end
    IMAGE_PROCESSOR_FUNCTION_NAME   the processor lambda the function forwards to
    REJECT_TOPIC_ARN                SNS topic for reject notifications
    KEEP_THRESHOLD / DROP_THRESHOLD the two score cutoffs (floats)
    MODEL_DIR                       (/opt/model) where visual.onnx + text.npz live
    AWS_REGION                      provided automatically by the Lambda runtime
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

# HEIC/HEIF decoding for Pillow 
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception as _heif_err:
    logger.warning("pillow-heif unavailable; HEIC/HEIF uploads will not decode: %s", _heif_err)

# Every value below is set by Terraform
RAW_PREFIX = os.environ.get("RAW_PREFIX", "images/raw/")
REJECTED_PREFIX = os.environ.get("REJECTED_PREFIX", "images/rejected/")
IMAGE_PROCESSOR_FUNCTION_NAME = os.environ.get("IMAGE_PROCESSOR_FUNCTION_NAME", "")
REJECT_TOPIC_ARN = os.environ.get("REJECT_TOPIC_ARN", "")

# Re-submitted images carry this marker and skip scoring, so a wrongly rejected card can't be rejected twice (boto3 strips the x-amz-meta- prefix)
FORCE_PROCESS_META = "force-process"

# Verdict tresholds
KEEP_THRESHOLD = float(os.environ.get("KEEP_THRESHOLD", "0.50"))
DROP_THRESHOLD = float(os.environ.get("DROP_THRESHOLD", "0.20"))

MODEL_DIR = os.environ.get("MODEL_DIR", "/opt/model")
VISUAL_ONNX = os.path.join(MODEL_DIR, "visual.onnx")
TEXT_NPZ = os.path.join(MODEL_DIR, "text.npz")

# ViT-B/32 input resolution + CLIP normalisation constants
IMAGE_SIZE = 224
CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

# AWS clients
s3 = boto3.client("s3")
sns = boto3.client("sns")
lambda_client = boto3.client("lambda")  # async-invokes the image-processor

# Model artifacts
# Load the ONNX image encoder + precomputed label vectors ONCE per container, reused across warm invocations
_SESSION = ort.InferenceSession(VISUAL_ONNX, providers=["CPUExecutionProvider"])
_INPUT_NAME = _SESSION.get_inputs()[0].name
_TEXT = np.load(TEXT_NPZ, allow_pickle=True)
_TEXT_EMBEDS = _TEXT["embeds"].astype(np.float32)
_LOGIT_SCALE = float(_TEXT["logit_scale"])

# Two label groups (build_onnx.py); ordering is [AWS..., competitor..., noncard...]
_CARD_COUNT = int(_TEXT["card_count"])

# aws_count: first N labels are AWS-specific
_AWS_COUNT = int(_TEXT["aws_count"]) if "aws_count" in _TEXT else _CARD_COUNT


def preprocess_np(pil_img, size=IMAGE_SIZE):
    """
    Turns an image into the input tensor the encoder expects.

    Resizes, centre-crops to a square and normalises the pixels the same way open_clip does. 
    The long edge must use int(), not round(), or the scores drift.

    Args:
        pil_img: Input image as a PIL Image.
        size: Target square resolution in pixels.

    Returns:
        float32 array of shape (1, 3, size, size), ready for the encoder.
    """

    # Resize the shorter side to `size`, truncating the long edge
    img = pil_img.convert("RGB")
    w, h = img.size
    if w <= h:
        nw, nh = size, int(h * size / w)
    else:
        nh, nw = size, int(w * size / h)
    img = img.resize((nw, nh), Image.BICUBIC)

    # Centre-crop the resized image down to a square
    left, top = (nw - size) // 2, (nh - size) // 2
    img = img.crop((left, top, left + size, top + size))

    # Scale to 0-1 and apply the CLIP mean/std normalisation
    x = np.asarray(img).astype(np.float32) / 255.0
    x = (x - CLIP_MEAN) / CLIP_STD

    # Channel-first layout, wrapped as a batch of one
    x = np.transpose(x, (2, 0, 1))[None, ...]
    return np.ascontiguousarray(x, dtype=np.float32)


def p_card(raw_bytes):
    """
    Scores an image and returns how likely it is an AWS Builder Card and a card at all.

    The encoder turns the image into a vector, which is compared against every label vector. 
    A softmax makes all labels share one 100%, so a competitor label takes probability away from the AWS ones. 
    The two scores are sums over the AWS labels alone and over the AWS plus competitor labels.

    Args:
        raw_bytes: Encoded input image as downloaded from S3 (jpg/png/webp/bmp/tiff/heic).

    Returns:
        Tuple of (p_aws, p_any_card):
            p_aws      Probability the image is an AWS Builder Card, and the only score the function decides on. 
                       Low for other cards, which score on their own labels.
            p_any_card Probability it is a card of any kind. 
                       Never decides anything, only explains a rejection.
    """

    # Decode and preprocess into the encoder's expected tensor
    img = Image.open(io.BytesIO(raw_bytes))
    x = preprocess_np(img)

    # Encode the image and normalise, so the dot product below is a cosine similarity
    emb = _SESSION.run(None, {_INPUT_NAME: x})[0]          # (1, 512)
    emb = emb / np.linalg.norm(emb, axis=-1, keepdims=True)

    # Compare against every label vector and scale by the model's learned temperature
    logits = _LOGIT_SCALE * (emb @ _TEXT_EMBEDS.T)          # (1, num_labels)

    # Numerically stable softmax: all labels compete for one shared 100%
    z = logits - logits.max(axis=-1, keepdims=True)
    e = np.exp(z)
    probs = (e / e.sum(axis=-1, keepdims=True))[0]

    # Sum the mass over the two label groups the caller needs
    p_aws = float(probs[:_AWS_COUNT].sum())
    p_any_card = float(probs[:_CARD_COUNT].sum())
    return p_aws, p_any_card


def _forward_to_processor(record):
    """
    Hands an accepted upload to the image-processor lambda for the full cleanup pipeline.

    The record is wrapped back into an S3-shaped event, so the processor cannot tell it was not called by S3. 
    The invoke is asynchronous: this function returns straight away instead of waiting out the minute-or-two run, and Lambda still retries a failure on its own.
    A missing function name is logged instead of raised.

    Args:
        record: A single S3 event record, forwarded unmodified so the processor cannot tell it was not invoked by S3 directly.

    Returns:
        None.
    """

    # Without the target function name there is nothing to forward to
    if not IMAGE_PROCESSOR_FUNCTION_NAME:
        logger.error("[CardDetect] IMAGE_PROCESSOR_FUNCTION_NAME not set — cannot forward")
        return

    # Fire-and-forget: re-wrap the record as an S3-shaped event and invoke
    lambda_client.invoke(
        FunctionName=IMAGE_PROCESSOR_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps({"Records": [record]}).encode("utf-8"),
    )


def _quarantine(bucket, key):
    """
    Moves a rejected upload from the input prefix to the quarantine prefix.

    Nothing is triggered from the quarantine prefix.
    S3 b ucket is versioned, so a wrongly rejected image can be put back later.

    Args:
        bucket: Name of the S3 bucket holding the upload.
        key: Full key of the rejected object, under the raw prefix.

    Returns:
        The new key under the rejected prefix, used in the notification email.
    """

    # Same file name, different prefix
    rejected_key = REJECTED_PREFIX + key[len(RAW_PREFIX):]

    # S3 has no move operation, so copy to the new key and delete the original
    s3.copy_object(
        Bucket=bucket,
        Key=rejected_key,
        CopySource={"Bucket": bucket, "Key": key},
    )
    s3.delete_object(Bucket=bucket, Key=key)
    return rejected_key


def _publish_reject(bucket, key, rejected_key, p):
    """
    Sends the maintainer an email explaining that an upload was auto-rejected.

    It has its own SNS topic, because a rejected upload has no card record to review. 
    The email carries the score, both S3 locations and a CLI command to put the image back. 
    Sending is best effort, so a failed email cannot fail the rejection.

    Args:
        bucket: Name of the S3 bucket holding the upload.
        key: Original key of the rejected object, before it was quarantined.
        rejected_key: New key under the quarantine prefix, as returned by _quarantine().
        p: The AWS-card probability that fell below the drop cutoff.

    Returns:
        None.
    """

    # No topic = no email
    if not REJECT_TOPIC_ARN:
        logger.info("[CardDetect] REJECT_TOPIC_ARN not set, skipping reject email")
        return

    # Body
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
wrong, move it back into images/raw/ to reprocess it:

  aws s3 mv s3://{bucket}/{rejected_key} s3://{bucket}/{key} --region {os.environ.get("AWS_REGION", "eu-central-1")}
"""
    # Failed email must never fail the gate
    try:
        sns.publish(
            TopicArn=REJECT_TOPIC_ARN,
            Subject=f"Rejected upload: {name} (P(AWS)={p:.2f})",
            Message=message,
        )
        logger.info("[CardDetect] reject email sent for %s", key)
    except Exception as e:
        logger.exception("[CardDetect] failed to send reject email: %s", e)


def handler(event, context):
    """
    Lambda entry point: scores every uploaded image and either forwards or quarantines it.

    An image scoring at or above the drop cutoff goes to the image-processor; 
    below it, the image is quarantined and an email is sent. 
    The keep cutoff changes nothing, it only labels a forward confident or unsure. 
    A record that raises is logged and the rest still run.

    Args:
        event: S3 event notification; only its "Records" list is read.
        context: Lambda context object, unused.

    Returns:
        Dict with statusCode 200 and a JSON body listing one result per record: 
        the key, both scores, the verdict and the action taken, or the error.
    """

    logger.info("[CardDetect] keep>=%.2f drop<%.2f", KEEP_THRESHOLD, DROP_THRESHOLD)
    results = []

    for record in event.get("Records", []):
        # Unwrap the bucket and key from the S3 event record
        s3_info = record.get("s3", {})
        bucket = s3_info.get("bucket", {}).get("name")
        key = s3_info.get("object", {}).get("key")
        if not bucket or not key:
            logger.info("[CardDetect] missing bucket/key, skipping")
            continue

        # S3 percent-encodes keys in event notifications
        key = urllib.parse.unquote_plus(key)

        # Backup for the S3 suffix filter, so the function never scores its own by-products
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
            # Fetch the object; metadata is read before the body so the bypass can skip scoring
            obj = s3.get_object(Bucket=bucket, Key=key)

            # A re-submitted image is forwarded without scoring, so it cannot be rejected twice.
            if obj.get("Metadata", {}).get(FORCE_PROCESS_META):
                _forward_to_processor(record)
                logger.info(json.dumps({"key": key, "verdict": "forced", "action": "forwarded"}))
                results.append({"key": key, "verdict": "forced", "action": "forwarded"})
                continue

            # Score the image: p_aws drives the decision, p_any only explains a rejection
            raw = obj["Body"].read()
            p_aws, p_any = p_card(raw)

            # Real cards score ~0.3-0.9 and other cards ~0.00-0.02,cso the 0.20 cutoff sits in the gap between them.
            if p_aws >= DROP_THRESHOLD:
                # Looks like an AWS card -> forward.
                verdict = "aws-card" if p_aws >= KEEP_THRESHOLD else "aws-unsure"
                _forward_to_processor(record)
                action = "forwarded"
            else:
                # Low AWS score -> quarantine to images/rejected/ + notify. .
                verdict = "non-aws-card" if p_any >= DROP_THRESHOLD else "not-a-card"
                rejected_key = _quarantine(bucket, key)
                _publish_reject(bucket, key, rejected_key, p_aws)
                action = "rejected"

            # Structured log line on every invocation (observability).
            logger.info(json.dumps({
                "key": key, "p_aws": round(p_aws, 4), "p_card": round(p_any, 4),
                "verdict": verdict, "action": action,
            }))
            results.append({
                "key": key, "p_aws": round(p_aws, 4), "p_card": round(p_any, 4),
                "verdict": verdict, "action": action,
            })
        except Exception as e:
            logger.exception("[CardDetect] failed on %s: %s", key, e)
            results.append({"key": key, "status": "error", "error": str(e)})

    return {"statusCode": 200, "body": json.dumps({"results": results})}
