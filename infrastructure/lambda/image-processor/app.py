# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
"""Image-processor Lambda: turns a raw card photo into a straightened card and a pending review record.

card-detect invokes this function asynchronously with an S3 event for each upload under images/raw/
that may be a card. The handler cuts the card out of the photo with rembg (a BiRefNet model on
onnxruntime, CPU), straightens it with OpenCV, writes it to images/finished/<cardId>.png and keeps a
corners overlay next to the original. A Bedrock vision model drafts the card's title, effect and
description, which are stored in DynamoDB as a pending item for the maintainer's review, and an SNS
email announces the new card.

The rembg model is loaded once per container, at import, and reused by warm invocations; the
Dockerfile bakes it into the image under U2NET_HOME, so a cold start downloads nothing. HEIC and HEIF
photos, the iPhone default, decode through Pillow with the pillow-heif opener registered at import;
its wheel bundles libheif, so the image needs no OS package for it. Without pillow-heif a warning is
logged and the other formats still work.

Environment variables:
    REMBG_MODEL: The rembg model (default "birefnet-general-lite", which needs less memory and runs
        faster than "birefnet-general"). The Dockerfile sets it to the model it bakes in.
    MAX_DIM: The longest side, in pixels, that a photo is downscaled to before processing (default
        2048). Smaller photos are never upscaled; BiRefNet segments at 1024 pixels internally anyway.
    BACKGROUND: The fill around the card: "black" (default), "white", or "none" for transparency.
    RAW_PREFIX: The key prefix of uploads (default "images/raw/"); other keys are skipped.
    FINISHED_PREFIX: The key prefix of cleaned card images (default "images/finished/").
    DDB_TABLE: The DynamoDB table of cards (default "BuilderCards").
    BEDROCK_MODEL_ID: The Bedrock vision model that drafts the text (no default).
    BEDROCK_REGION: The region of the Bedrock client (default "eu-central-1").
    SNS_TOPIC_ARN: The topic of the "new card uploaded" email; empty skips the email.
    STATIC_CARDS_DIR, RAW_DOWNLOAD_DIR, CORNERS_DOWNLOAD_DIR: The local folders named in the email's
        download commands (default ".", the folder the commands are run in).
    REVIEW_URL: The review-editor link in the email.
"""

import os
import io
import re
import json
import logging
import urllib.parse
from datetime import datetime, timezone

import boto3
import numpy as np
import cv2
from rembg import remove, new_session
from PIL import Image

logger = logging.getLogger()
logger.setLevel(logging.INFO)

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception as _heif_err:  # pragma: no cover
    logger.warning("pillow-heif unavailable; HEIC/HEIF uploads will not decode: %s", _heif_err)

MODEL = os.environ.get("REMBG_MODEL", "birefnet-general-lite")
MAX_DIM = int(os.environ.get("MAX_DIM", "2048"))
BACKGROUND = os.environ.get("BACKGROUND", "black").lower()
RAW_PREFIX = os.environ.get("RAW_PREFIX", "images/raw/")
FINISHED_PREFIX = os.environ.get("FINISHED_PREFIX", "images/finished/")

DDB_TABLE = os.environ.get("DDB_TABLE", "BuilderCards")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "eu-central-1")

SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
STATIC_CARDS_DIR = os.environ.get("STATIC_CARDS_DIR", ".")
RAW_DOWNLOAD_DIR = os.environ.get("RAW_DOWNLOAD_DIR", ".")
CORNERS_DOWNLOAD_DIR = os.environ.get("CORNERS_DOWNLOAD_DIR", ".")
REVIEW_URL = os.environ.get("REVIEW_URL", "")

s3 = boto3.client("s3")
ddb = boto3.client("dynamodb")
bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
sns = boto3.client("sns")

SESSION = new_session(MODEL)


def get_mask(raw_bytes, session):
    """Segment the card in a photo and return a binary mask of it.

    The mask holds only the largest foreground region, with small holes and specks removed.

    Args:
        raw_bytes: The encoded photo, at the size the rest of the pipeline works on.
        session: The rembg session from new_session(), which holds the loaded segmentation model.

    Returns:
        A uint8 mask the size of the photo: 255 on the card, 0 elsewhere. It is all zeros when no
        foreground is found.
    """
    cutout = Image.open(io.BytesIO(remove(raw_bytes, session=session))).convert("RGBA")

    alpha = np.array(cutout)[:, :, 3]
    _, mask = cv2.threshold(alpha, 127, 255, cv2.THRESH_BINARY)

    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)

    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n > 1:
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        mask = np.where(labels == largest, 255, 0).astype(np.uint8)
    return mask


def _rotate_pts(pts, M):
    """Apply a 2x3 affine transform to a set of points.

    Args:
        pts: An (N, 2) array of x, y points.
        M: A 2x3 affine matrix, such as one from cv2.getRotationMatrix2D or cv2.invertAffineTransform.

    Returns:
        An (N, 2) float array of the transformed points.
    """
    ones = np.ones((len(pts), 1), dtype=np.float32)
    h = np.hstack([pts.astype(np.float32), ones])

    return (M @ h.T).T


def _fit_line(pts):
    """Fit a straight line through a set of points by least squares.

    Args:
        pts: An (N, 2) array of x, y points along one edge of the card.

    Returns:
        (x0, y0, vx, vy) as floats: a point on the line and the line's unit direction vector.
    """
    vx, vy, x0, y0 = cv2.fitLine(
        pts.astype(np.float32),
        cv2.DIST_L2,
        0,
        0.01,
        0.01
    ).flatten()

    return (float(x0), float(y0), float(vx), float(vy))


def _intersect(l1, l2):
    """Intersect two lines given as a point and a direction.

    Args:
        l1: The first line as (x0, y0, vx, vy), as _fit_line returns it.
        l2: The second line, in the same form.

    Returns:
        The intersection as a float32 array of shape (2,), or None when the lines are exactly
        parallel. Nearly parallel lines still give a point, possibly far outside the image.
    """
    x1, y1, vx1, vy1 = l1
    x2, y2, vx2, vy2 = l2

    A = np.array([[vx1, -vx2], [vy1, -vy2]], dtype=np.float64)
    b = np.array([x2 - x1, y2 - y1], dtype=np.float64)

    try:
        t, _ = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        return None

    return np.array([x1 + t * vx1, y1 + t * vy1], dtype=np.float32)


def find_quad(mask):
    """Find the four corners of the card in its mask.

    The corners are where straight lines fitted to the card's four edges meet, so rounded card corners
    still give sharp ones. Each line is fitted to the middle of its edge only, leaving out 18 % of the
    side at each end, where the corner bends away, and needs at least 15 points. Nearly parallel
    edges meet far away, so a corner more than 10 % of the photo's longer side outside the photo also
    counts as a failure; the margin keeps a card whose corner is cut off by the photo's edge. When that
    fails, a convex four-point polygon approximation of the largest contour is used, and as a last
    resort the minimum-area rectangle around it.

    Args:
        mask: The card's binary mask, from get_mask.

    Returns:
        A float32 (4, 2) array of corner points in the mask's pixel coordinates, in no fixed order, or
        None when the mask has no contour.
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None

    c = max(contours, key=cv2.contourArea)
    pts = c.reshape(-1, 2).astype(np.float32)

    (cx, cy), (rw, rh), ang = cv2.minAreaRect(c)
    M = cv2.getRotationMatrix2D((cx, cy), ang, 1.0)
    P = _rotate_pts(pts, M)

    minx, maxx = P[:, 0].min(), P[:, 0].max()
    miny, maxy = P[:, 1].min(), P[:, 1].max()
    W, H = maxx - minx, maxy - miny
    bx, by = 0.18 * W, 0.18 * H

    cxlo, cxhi = minx + bx, maxx - bx
    cylo, cyhi = miny + by, maxy - by

    top    = P[(P[:, 1] < miny + by) & (P[:, 0] > cxlo) & (P[:, 0] < cxhi)]
    bottom = P[(P[:, 1] > maxy - by) & (P[:, 0] > cxlo) & (P[:, 0] < cxhi)]
    left   = P[(P[:, 0] < minx + bx) & (P[:, 1] > cylo) & (P[:, 1] < cyhi)]
    right  = P[(P[:, 0] > maxx - bx) & (P[:, 1] > cylo) & (P[:, 1] < cyhi)]

    if min(len(top), len(bottom), len(left), len(right)) >= 15:
        lt, lb = _fit_line(top), _fit_line(bottom)
        ll, lr = _fit_line(left), _fit_line(right)
        tl, tr = _intersect(lt, ll), _intersect(lt, lr)
        br, bl = _intersect(lb, lr), _intersect(lb, ll)
        if all(p is not None for p in (tl, tr, br, bl)):
            corners_rot = np.array([tl, tr, br, bl], dtype=np.float32)
            Minv = cv2.invertAffineTransform(M)
            corners = _rotate_pts(corners_rot, Minv).astype(np.float32)
            h, w = mask.shape[:2]
            margin = 0.1 * max(h, w)
            if np.all((corners >= -margin) & (corners <= np.array([w - 1, h - 1]) + margin)):
                return corners

    peri = cv2.arcLength(c, True)
    for f in [0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10]:
        approx = cv2.approxPolyDP(c, f * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            return approx.reshape(4, 2).astype(np.float32)

    return cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)


def order_corners(pts):
    """Sort four corner points into top-left, top-right, bottom-right, bottom-left order.

    The points are sorted by their angle around their centroid, which is clockwise on screen because
    image y points down, and the list starts at the point with the smallest x + y. Unlike picking each
    corner by the sum or the difference of its coordinates, this uses every point exactly once, also
    for a card turned near 45 degrees; for an upright card both give the same order.

    Args:
        pts: A (4, 2) array of corner points in any order, as find_quad returns them.

    Returns:
        A float32 (4, 2) array: top-left, top-right, bottom-right, bottom-left.
    """
    c = pts.mean(axis=0)
    ring = pts[np.argsort(np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0]))]
    start = int(np.argmin(ring.sum(axis=1)))

    return np.float32(np.roll(ring, -start, axis=0))


def rectify(img_bgr, quad, mask, background="white"):
    """Warp the card to a straight, front-facing rectangle and fill the area around it.

    Args:
        img_bgr: The photo as a BGR OpenCV image, the same size as mask.
        quad: The card's four corners in any order, from find_quad.
        mask: The card's binary mask, from get_mask.
        background: "white" or "black" fills the area outside the card with that colour, and "none"
            makes it transparent. Any other value logs a warning and fills black, so a typo in the
            setting shows up in the log instead of passing silently.

    Returns:
        The straightened card, sized to its longer opposite edges: a BGR image, or BGRA for "none".
        None when either side would be shorter than 2 pixels.
    """
    src = order_corners(quad)
    (tl, tr, br, bl) = src

    w = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    h = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    if w < 2 or h < 2:
        return None

    dst = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
    M = cv2.getPerspectiveTransform(src, dst)

    warped = cv2.warpPerspective(img_bgr, M, (w, h))

    warped_mask = cv2.warpPerspective(mask, M, (w, h), flags=cv2.INTER_NEAREST)
    _, warped_mask = cv2.threshold(warped_mask, 127, 255, cv2.THRESH_BINARY)
    er = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    warped_mask = cv2.erode(warped_mask, er, iterations=1)

    if background == "none":
        out = cv2.cvtColor(warped, cv2.COLOR_BGR2BGRA)
        out[:, :, 3] = warped_mask
        return out

    FILLS = {"white": 255, "black": 0}
    if background not in FILLS:
        logger.warning(
            "[ImageProcessor] unknown BACKGROUND=%r (expected white, black or none) - falling back to black",
            background,
        )
    fill = FILLS.get(background, 0)
    outside = warped_mask == 0
    warped[outside] = fill
    return warped


def downscale(img_bgr, max_dim):
    """Shrink an image so its longest side is at most max_dim, keeping the aspect ratio.

    INTER_AREA interpolation is used because it gives the best quality when shrinking.

    Args:
        img_bgr: An OpenCV image.
        max_dim: The longest side allowed, in pixels.

    Returns:
        The same image object when it already fits (it is never upscaled), otherwise a resized copy.
    """
    h, w = img_bgr.shape[:2]
    longest = max(h, w)

    if longest <= max_dim:
        return img_bgr

    scale = max_dim / float(longest)

    return cv2.resize(
        img_bgr,
        (int(round(w * scale)), int(round(h * scale))),
        interpolation=cv2.INTER_AREA,
    )


def draw_corners(img_bgr, quad):
    """Draw the card's outline and corners on a copy of the photo, to check the corner detection by eye.

    Args:
        img_bgr: The photo as a BGR OpenCV image.
        quad: The card's four corners in any order, from find_quad.

    Returns:
        A copy of the photo with the outline in red and the corners as green dots (the colours are
        given in OpenCV's BGR order); the input image is not changed.
    """
    vis = img_bgr.copy()

    pts = order_corners(quad).astype(int)

    cv2.polylines(vis, [pts.reshape(-1, 1, 2)], True, (0, 0, 255), 3)

    for (x, y) in pts:
        cv2.circle(vis, (int(x), int(y)), 8, (0, 255, 0), -1)

    return vis


def encode_corners(img_bgr, quad):
    """Encode the corners overlay from draw_corners as a PNG.

    Args:
        img_bgr: The photo as a BGR OpenCV image.
        quad: The card's four corners in any order, from find_quad.

    Returns:
        The PNG bytes, or None when OpenCV cannot encode the image.
    """
    ok, buf = cv2.imencode(".png", draw_corners(img_bgr, quad))
    return buf.tobytes() if ok else None


def _decode_bgr(raw_bytes):
    """Decode uploaded image bytes into a BGR OpenCV image.

    OpenCV decodes the common formats (JPEG, PNG, WebP, BMP, TIFF). It has no HEIC/HEIF codec, so
    anything it cannot read falls back to Pillow, which reads HEIC/HEIF through the pillow-heif
    opener registered at import.

    Args:
        raw_bytes: The uploaded file's bytes.

    Returns:
        The image as a BGR array, or None when neither OpenCV nor Pillow can decode it (the Pillow
        error is logged as a warning).
    """
    img = cv2.imdecode(np.frombuffer(raw_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is not None:
        return img

    try:
        pil = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        return cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
    except Exception as e:
        logger.warning("[ImageProcessor] Pillow fallback decode failed: %s", e)
        return None


def process_image(raw_bytes):
    """Turn an uploaded photo into a straightened card PNG and a corners-overlay PNG.

    A photo larger than MAX_DIM is downscaled first. The working image is re-encoded for rembg instead
    of passing the original bytes, so the mask lines up pixel for pixel with the image that is warped,
    and the overlay is drawn on that same image. The model in SESSION does the segmentation, and
    BACKGROUND sets the fill around the card.

    Args:
        raw_bytes: The uploaded file's bytes, in any format _decode_bgr reads.

    Returns:
        (card_png, corners_png), both PNG bytes. card_png is None when no card could be cut out;
        corners_png is None when no card outline was found or the overlay could not be encoded.

    Raises:
        ValueError: The bytes cannot be decoded as an image, or a PNG encoding fails.
    """
    img = _decode_bgr(raw_bytes)
    if img is None:
        raise ValueError("could not decode image bytes")

    h0, w0 = img.shape[:2]
    img = downscale(img, MAX_DIM)
    h1, w1 = img.shape[:2]
    if (w1, h1) != (w0, h0):
        logger.info("[ImageProcessor] downscaled %dx%d -> %dx%d (MAX_DIM=%d)", w0, h0, w1, h1, MAX_DIM)
    else:
        logger.info("[ImageProcessor] input %dx%d (<= MAX_DIM=%d, left as-is)", w0, h0, MAX_DIM)

    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise ValueError("PNG re-encode failed")
    work_bytes = buf.tobytes()

    mask = get_mask(work_bytes, SESSION)
    quad = find_quad(mask)
    if quad is None:
        return None, None

    corners_png = encode_corners(img, quad)

    out = rectify(img, quad, mask, BACKGROUND)
    if out is None:
        return None, corners_png

    ok, buf = cv2.imencode(".png", out)
    if not ok:
        raise ValueError("PNG encode failed")
    return buf.tobytes(), corners_png


def finished_key(card_id):
    """Build the S3 key of the cleaned card image.

    Args:
        card_id: The card's ID, from parse_meta.

    Returns:
        FINISHED_PREFIX followed by "<card_id>.png", for example "images/finished/<cardId>.png".
    """
    return f"{FINISHED_PREFIX}{card_id}.png"


def corners_key(raw_key):
    """Build the S3 key of the corners overlay, next to the original upload.

    The key ends in "__corners.png", which the handler skips, and the S3 trigger fires only on ".jpg"
    keys, so writing the overlay into the upload folder never starts processing again.

    Args:
        raw_key: The S3 key of the uploaded photo, such as "images/raw/<stem>.jpg".

    Returns:
        The key with its extension replaced by "__corners.png", such as
        "images/raw/<stem>__corners.png".
    """
    base = raw_key.rsplit(".", 1)[0] if "." in raw_key else raw_key

    return f"{base}__corners.png"


# Every part but the last ends with a space: adjacent string literals are joined without one.
EXTRACT_SYSTEM = (
    "You are a precise data-extraction assistant for AWS Builder Cards. "
    "Read the card image and return ONLY a JSON object, no commentary. "
    "Do not return markdown. "
    "Do not return explanations. "
    "Do not use code fences."
)


EXTRACT_PROMPT = """Return ONLY valid JSON.

The JSON must have exactly these keys:

{
"title": "",
"effect": "",
"description": ""
}

Read the card from top to bottom.

TASK 1: TITLE
The title is the card name. It can have two parts.

Part 1 - the text in the title bar at the top of the card.
Always present.
Read it in full, exactly as printed.

Part 2 - a qualifier printed in its own banner inside the artwork, below the title bar.
Only some cards have this. Examples of what it looks like: ASSOCIATE, PROFESSIONAL, FOUNDATIONAL, SPECIALTY.

If a banner like that is visible, the title is Part 1 followed by Part 2, written in normal capitalisation:
"AWS certified Solutions Architect" + "ASSOCIATE" -> "AWS certified Solutions Architect Associate"

If no such banner is visible, the title is Part 1 alone.
Do not add a qualifier that is not printed on the card.

Examples: "AWS Cloud Practitioner", "David", "AWS certified Solutions Architect Professional".
If you cannot read it, use "".

TASK 2: EFFECT
The effect is the gameplay area in the middle of the card.

The effect area may contain small icons.
Not all icon types are always present on a card.
A card may have: 0 icons, or 1 icon, or 2 icons or all 3 icons.
Only use icons that are actually visible on this card.

There are exactly 3 possible icon types:

ICON TYPE 1: CREDIT
Visual appearance: orange circle contains a white number. That number has NO plus sign.
Example: orange circle with "1"
This icon can be small, so look carefully.
Meaning: "Get N credit" if N = 1. "Get N credits" if N > 1.

ICON TYPE 2: DRAW CARDS
Visual appearance: black rounded rectangle contains a white number. the number ALWAYS has a plus sign, like "+1" or "+2"
This icon can be small, so look carefully.
Meaning: "Draw N card from your Resources Pile" if N = 1. "Draw N cards from your Resources Pile" if N > 1.

ICON TYPE 3: CLOUD ADOPTION EFFECT
Visual appearance: small white cloud shape with black outline contains a black number. the number ALWAYS has a plus sign, like "+1" or "+2".
This icon can be small, so look carefully.
Meaning: "Use N cloud adoption effect" if N = 1. "Use N cloud adoption effects" if N > 1

IMPORTANT ICON RULES:
If a number has NO plus sign and is inside an orange circle, it means credits.
If a number has a plus sign and is inside a dark rounded rectangle, it means draw cards.
If a number has a plus sign and is inside a white cloud outline, it means cloud adoption effects.
A plus sign never means credits.
Do not invent missing icons.
Do not mention credits unless an orange circle is visible.
Do not mention drawing cards unless a dark rounded rectangle is visible.
Do not mention cloud adoption effects unless a white cloud icon is visible.

TEXT AFTER ICONS:
After each small icon, there may be extra text.
Sometimes there is text after the icon.
Sometimes there is no text after the icon.
If text appears after the icon, append that text to the icon meaning.
If the text wraps across multiple lines, merge it into one sentence.
If there is no text after the icon, use only the icon meaning.

Read all visible effect icons from top to bottom.
For each visible icon, create one effect sentence.
Join multiple effect sentences using \\n inside the JSON string.

TASK 3: DESCRIPTION
The description is the smaller text near the bottom of the card.
Include any URL if visible, for example "buildercards.aws/kiro".
If there is no description or it cannot be read, use "".

OUTPUT RULES:
Return only valid JSON.
Use exactly the keys "title", "effect", "description".
Do not add any other keys.
Do not explain your answer.
If a field cannot be read, use "".

LINKS:
Any internet link (URL) you spot, you must write in this format: [link](link).
example: You see: buildercards.aws/certification -> you write: [buildercards.aws/certification](buildercards.aws/certification)

BOLD TEXT:
If you spot text in bold, write it as for markdown files - that means like this: **this is bold text**
"""


def _parse_json(text):
    """Read the card fields out of the vision model's answer.

    The answer may be wrapped in Markdown code fences or surrounded by other text: the part from the
    first "{" to the last "}" is parsed.

    Args:
        text: The model's text answer.

    Returns:
        A dict with exactly the keys "title", "effect" and "description", each a string; a key the
        model left out or set to null becomes "".

    Raises:
        ValueError: The answer holds no valid JSON (json.JSONDecodeError is a ValueError).
        AttributeError: The answer is valid JSON but not an object.
    """
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]

    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j != -1:
        t = t[i:j + 1]

    data = json.loads(t)
    return {
        "title": str(data.get("title", "") or ""),
        "effect": str(data.get("effect", "") or ""),
        "description": str(data.get("description", "") or ""),
    }


def extract_card_fields(png_bytes):
    """Have the Bedrock vision model draft the card's title, effect and description.

    The role sentence is sent only once, in the system prompt EXTRACT_SYSTEM; the user message holds
    the task, EXTRACT_PROMPT, and the image.

    Args:
        png_bytes: The straightened card as PNG bytes.

    Returns:
        The drafted fields, as _parse_json returns them.

    Raises:
        botocore.exceptions.ClientError: The Bedrock call fails.
        ValueError: The answer holds no valid JSON.
    """
    resp = bedrock.converse(
        modelId=BEDROCK_MODEL_ID,
        system=[{"text": EXTRACT_SYSTEM}],
        messages=[{
            "role": "user",
            "content": [
                {"text": EXTRACT_PROMPT},
                {"image": {"format": "png", "source": {"bytes": png_bytes}}},
            ],
        }],
        inferenceConfig={"maxTokens": 1200, "temperature": 0.0},
    )

    text = resp["output"]["message"]["content"][0]["text"]
    return _parse_json(text)


def parse_meta(raw_key):
    """Split an upload's S3 key into its event name, year and card ID.

    get-upload-url names uploads images/raw/<event>_<year>_<cardId>.jpg, with no "_" in the event.

    Args:
        raw_key: The S3 key of the uploaded photo.

    Returns:
        (event, year, card_id) as strings, with hyphens in the event turned into spaces. A file name
        with fewer than three "_"-separated parts gives ("", "", <file name without extension>).
    """
    filename = raw_key.split("/")[-1]
    stem = filename.rsplit(".", 1)[0]

    parts = stem.split("_")
    if len(parts) >= 3:
        card_id = parts[-1]
        year = parts[-2]
        event = " ".join(parts[:-2]).replace("-", " ")
    else:
        card_id, year, event = stem, "", ""

    return event, year, card_id


def slugify(text):
    """Turn text into a URL slug: lowercase ASCII letters and digits joined by single hyphens.

    Args:
        text: The text to turn into a slug; None counts as empty.

    Returns:
        The slug, for example "aws-summit-hong-kong-2025" for "AWS Summit Hong Kong 2025". It is ""
        when the text has no ASCII letters or digits.
    """
    s = (text or "").strip().lower()

    s = re.sub(r"[^a-z0-9]+", "-", s)

    return s.strip("-")


def write_card_item(card_id, event, year, raw_key, fin_key, fields, uploader=""):
    """Store a new card in DynamoDB as a pending draft for review.

    The model's title, effect and description, and a slug made from the title and the year, go into
    the *_ai_draft attributes; the approved attributes start empty and are filled in at review. The
    year is added to the slug only when the title does not already contain it, so a title such as
    "AWS Summit Katowice 2025" does not give "aws-summit-katowice-2025-2025". The write replaces any
    existing item with the same cardId.

    Args:
        card_id: The card's ID, from parse_meta.
        event: The event name, from parse_meta.
        year: The event year, from parse_meta; may be empty.
        raw_key: The S3 key of the uploaded photo.
        fin_key: The S3 key of the cleaned card image.
        fields: The drafted fields, as extract_card_fields returns them; a missing key counts as "".
        uploader: The uploader's name from the upload's metadata; "" when none was given.

    Returns:
        None. Writes the item, with status "pending", to the DDB_TABLE table.

    Raises:
        botocore.exceptions.ClientError: The DynamoDB write fails.
    """
    now = datetime.now(timezone.utc).isoformat()

    title_draft = fields.get("title", "")
    year_str = str(year).strip()
    slug_src = f"{title_draft} {year_str}" if year_str and year_str not in title_draft else title_draft
    slug_draft = slugify(slug_src)

    ddb.put_item(
        TableName=DDB_TABLE,
        Item={
            "cardId": {"S": card_id},
            "slug": {"S": ""},
            "slug_ai_draft": {"S": slug_draft},
            "weight": {"S": ""},
            "event": {"S": event or ""},
            "year": {"S": str(year or "")},
            "uploader": {"S": uploader or ""},
            "category": {"S": "collectibles"},
            "subcategory": {"S": ""},
            "title": {"S": ""},
            "title_ai_draft": {"S": fields.get("title", "")},
            "effect": {"S": ""},
            "effect_ai_draft": {"S": fields.get("effect", "")},
            "description": {"S": ""},
            "description_ai_draft": {"S": fields.get("description", "")},
            "rawKey": {"S": raw_key},
            "finishedKey": {"S": fin_key},
            "status": {"S": "pending"},
            "createdAt": {"S": now},
            "updatedAt": {"S": now},
        },
    )


def publish_upload_notification(bucket, finished_key, card_id, event_name, year, title_draft, raw_key, event_time=""):
    """Send the "new card uploaded" message to the SNS topic, which emails the maintainer.

    The message holds the card's metadata, aws s3 cp commands that download the original photo into
    RAW_DOWNLOAD_DIR, the corners overlay into CORNERS_DOWNLOAD_DIR and the cleaned image into
    STATIC_CARDS_DIR, and the REVIEW_URL link. It is best-effort: a failed publish is logged and never
    fails the card.

    Args:
        bucket: The S3 bucket of the images.
        finished_key: The S3 key of the cleaned card image.
        card_id: The card's ID.
        event_name: The event name; "Unknown" is shown when it is empty.
        year: The event year; "Unknown" is shown when it is empty.
        title_draft: The model's draft title; "(none)" is shown when it is empty.
        raw_key: The S3 key of the uploaded photo; when it is empty, only the cleaned image's
            download command is included.
        event_time: The upload time from the S3 event record; "Unknown" is shown when it is empty.

    Returns:
        None. Publishes the message to SNS_TOPIC_ARN, or only logs that it skipped when SNS_TOPIC_ARN
        is empty.
    """
    if not SNS_TOPIC_ARN:
        logger.info("[ImageProcessor] SNS_TOPIC_ARN not set, skipping notification")
        return

    finished_cmd = (
        "# 3) Cropped / finished card -> Hugo static/cards\n"
        f"cd {STATIC_CARDS_DIR}\n"
        f"aws s3 cp s3://{bucket}/{finished_key} {card_id}.png"
    )

    if raw_key:
        raw_name = raw_key.split("/")[-1]
        corners_obj_key = corners_key(raw_key)
        corners_name = corners_obj_key.split("/")[-1]
        original_cmd = (
            "# 1) Original photo\n"
            f"aws s3 cp s3://{bucket}/{raw_key} {RAW_DOWNLOAD_DIR}/{raw_name}"
        )
        corners_cmd = (
            "# 2) With detected corners\n"
            f"aws s3 cp s3://{bucket}/{corners_obj_key} {CORNERS_DOWNLOAD_DIR}/{corners_name}"
        )
        download_block = "\n\n".join([original_cmd, corners_cmd, finished_cmd])
    else:
        download_block = (
            finished_cmd
            + "\n\n(original + corners links unavailable: no rawKey on this card record)"
        )

    message = f"""New Builder Card uploaded!

=== IMAGE INFO ===
S3 Location: s3://{bucket}/{finished_key}
Image Name: {finished_key.split('/')[-1]}
Card ID: {card_id}

=== METADATA ===
Event Name: {event_name or "Unknown"}
Year: {year or "Unknown"}
AI Title (draft): {title_draft or "(none)"}
Upload Time: {event_time or "Unknown"}

=== DOWNLOAD COMMANDS ===
{download_block}

=== ACCESS THE CARD DETAILS ===
{REVIEW_URL or "(review URL not configured)"}"""

    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=f"New Builder Card: {event_name or 'Unknown'} ({year or 'Unknown'})",
            Message=message,
        )
        logger.info("[ImageProcessor] SNS notification sent for %s", finished_key)
    except Exception as e:  # noqa: BLE001 - non-fatal
        logger.exception("[ImageProcessor] failed to send SNS notification: %s", e)


def handler(event, context):
    """Process each uploaded photo in an S3 event into a pending card.

    card-detect invokes this function asynchronously with one S3 event record per upload it lets
    through. For each record, the photo becomes a straightened card, a corners overlay is written next
    to the upload, Bedrock drafts the text, a pending DynamoDB item is written, the cleaned image goes
    under FINISHED_PREFIX and an SNS email announces the card.

    Keys outside RAW_PREFIX, ".keep" folder placeholders and "__corners.png" overlays are skipped, so
    the overlay this function writes into the upload folder is never processed as a card. The
    uploader's name is the object's "uploader" metadata: get-upload-url signs it as the
    x-amz-meta-uploader field of the pre-signed POST, and boto3 returns user metadata keys lowercased
    and without the x-amz-meta- prefix.

    The Bedrock draft, the overlay write and the email are best-effort: a failure is logged and the
    record goes on. Any other failure ends that record with status "error", and the next record is
    processed. The DynamoDB item is written before the cleaned image and the email, so a failed write
    never announces a card that the review list does not show.

    When any record failed, the function raises after the loop. card-detect invokes it
    asynchronously, and Lambda retries an asynchronous event only when the function fails, so
    returning normally would drop a card after a temporary S3 error for good. A retry is safe: the
    cardId comes from the upload's key, so it rewrites the same card rather than adding one; it does
    cost a second Bedrock call.

    Args:
        event: An S3 event, {"Records": [...]}; each record has s3.bucket.name, s3.object.key
            (URL-encoded, as S3 sends it) and optionally eventTime.
        context: The Lambda context object; not used.

    Returns:
        {"statusCode": 200, "body": <JSON string>}, where the body is {"results": [...]} with one
        entry per processed record: {"key": ..., "finished": ..., "status": "ok"} or
        {"key": ..., "status": "no_card"}. Skipped records get no entry, so an empty list means
        nothing was processed.

    Raises:
        RuntimeError: One or more records failed; the message lists their keys.
    """
    logger.info("[ImageProcessor] model=%s background=%s", MODEL, BACKGROUND)
    results = []

    for record in event.get("Records", []):
        s3_info = record.get("s3", {})
        bucket = s3_info.get("bucket", {}).get("name")
        key = s3_info.get("object", {}).get("key")
        if not bucket or not key:
            logger.info("[ImageProcessor] missing bucket/key, skipping")
            continue

        key = urllib.parse.unquote_plus(key)

        if key.endswith(".keep"):
            logger.info("[ImageProcessor] skipping .keep placeholder")
            continue
        if key.endswith("__corners.png"):
            logger.info("[ImageProcessor] skipping corners-overlay image (not a card)")
            continue
        if not key.startswith(RAW_PREFIX):
            logger.info("[ImageProcessor] key not under RAW_PREFIX=%s, skipping", RAW_PREFIX)
            continue

        logger.info("[ImageProcessor] processing s3://%s/%s", bucket, key)
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            raw = obj["Body"].read()

            uploader = obj.get("Metadata", {}).get("uploader", "")

            png, corners_png = process_image(raw)
            if png is None:
                logger.warning("[ImageProcessor] no card detected in %s", key)
                results.append({"key": key, "status": "no_card"})
                continue

            if corners_png is not None:
                ck = corners_key(key)
                try:
                    s3.put_object(Bucket=bucket, Key=ck, Body=corners_png, ContentType="image/png")
                    logger.info("[ImageProcessor] wrote corners overlay s3://%s/%s", bucket, ck)
                except Exception as ex:  # noqa: BLE001 - non-fatal
                    logger.exception("[ImageProcessor] corners write failed: %s", ex)

            event_name, year, card_id = parse_meta(key)
            out_key = finished_key(card_id)

            fields = {"title": "", "effect": "", "description": ""}
            try:
                fields = extract_card_fields(png)
                logger.info("[ImageProcessor] extracted title=%r", fields.get("title", ""))
            except Exception as ex:  # noqa: BLE001
                logger.exception("[ImageProcessor] Bedrock extraction failed: %s", ex)
            write_card_item(card_id, event_name, year, key, out_key, fields, uploader)
            logger.info("[ImageProcessor] wrote DynamoDB item cardId=%s (pending)", card_id)

            s3.put_object(
                Bucket=bucket,
                Key=out_key,
                Body=png,
                ContentType="image/png",
            )
            logger.info("[ImageProcessor] wrote s3://%s/%s", bucket, out_key)

            publish_upload_notification(
                bucket, out_key, card_id, event_name, year,
                fields.get("title", ""), key, record.get("eventTime", ""),
            )

            results.append({"key": key, "finished": out_key, "status": "ok"})
        except Exception as e:  # noqa: BLE001 - log and continue to next record
            logger.exception("[ImageProcessor] failed on %s: %s", key, e)
            results.append({"key": key, "status": "error", "error": str(e)})

    failed = [r["key"] for r in results if r.get("status") == "error"]
    if failed:
        raise RuntimeError(f"{len(failed)} record(s) failed, raising so Lambda retries the event: {failed}")
    return {"statusCode": 200, "body": json.dumps({"results": results})}
