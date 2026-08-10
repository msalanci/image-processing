# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Michal Salanci

"""Turns a crooked photo of an AWS Builder Card into a clean, straight image.

The card-detect gate invokes this lambda with the S3 event for a new upload. 
A segmentation model marks which pixels are the card, OpenCV finds its four corners and straightens it and the background is cut away. 
The finished image then goes to a Bedrock vision model, which drafts the card's text into DynamoDB, and an email says the card is ready to review.

Nothing here is published: every field is written as a draft, and the approved fields are left empty for a human to fill in.

Functions:
    handler: Entry point; processes each uploaded image in the S3 event.
    process_image: Runs the whole cleanup and returns the finished PNG.
    extract_card_fields: Reads the card's text with a Bedrock vision model.

Environment (set by Terraform):
    REMBG_MODEL             segmentation model name (weights are baked into the image)
    MAX_DIM                 longest side allowed before the photo is downscaled
    BACKGROUND              fill behind the card: black, white or none
    RAW_PREFIX              prefix uploads arrive under
    FINISHED_PREFIX         prefix the cleaned images are written to
    DDB_TABLE               card table the draft record is written to
    BEDROCK_MODEL_ID        vision model that reads the card
    BEDROCK_REGION          region the Bedrock call goes to
    SNS_TOPIC_ARN           topic for the "go review it" email
    REVIEW_URL              link to the review editor, included in that email
    AWS_REGION              provided automatically by the Lambda runtime
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

# HEIC/HEIF decoding for Pillow
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception as _heif_err:
    logger.warning("pillow-heif unavailable; HEIC/HEIF uploads will not decode: %s", _heif_err)

MODEL = os.environ.get("REMBG_MODEL", "birefnet-general-lite")

# Photos longer than this are downscaled to bound memory; smaller ones are left alone
MAX_DIM = int(os.environ.get("MAX_DIM", "2048"))
BACKGROUND = os.environ.get("BACKGROUND", "black").lower()  # black, white or none
RAW_PREFIX = os.environ.get("RAW_PREFIX", "images/raw/")
FINISHED_PREFIX = os.environ.get("FINISHED_PREFIX", "images/finished/")

DDB_TABLE = os.environ.get("DDB_TABLE", "BuilderCards")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "eu-central-1")

# Topic for the "new card uploaded, go review it" email
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
STATIC_CARDS_DIR = os.environ.get("STATIC_CARDS_DIR", "web/awsbuildercards/static/cards")
RAW_DOWNLOAD_DIR = os.environ.get("RAW_DOWNLOAD_DIR", "card_pictures/RAW")
CORNERS_DOWNLOAD_DIR = os.environ.get("CORNERS_DOWNLOAD_DIR", "card_pictures/CORNERS")
REVIEW_URL = os.environ.get("REVIEW_URL", "")

s3 = boto3.client("s3")
ddb = boto3.client("dynamodb")
bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
sns = boto3.client("sns")

# Loaded once per container from weights baked into the image, then reused while it stays warm
SESSION = new_session(MODEL)


def get_mask(raw_bytes, session):
    """
    Marks which pixels are the card and returns them as a black and white stencil.

    The segmentation model scores every pixel, the score is cut into card or background at the midpoint, 
    then holes and specks are cleaned up and only the largest blob is kept.

    Args:
        raw_bytes: Encoded input image.
        session: Initialized rembg/BiRefNet inference session.

    Returns:
        Binary uint8 mask with foreground pixels equal to 255 and background pixels equal to 0.
    """

    # BiRefNet segmentation
    cutout = Image.open(io.BytesIO(remove(raw_bytes, session=session))).convert("RGBA")
    
    # Extract foreground mask
    alpha = np.array(cutout)[:, :, 3]
    _, mask = cv2.threshold(alpha, 127, 255, cv2.THRESH_BINARY)
    
    # Remove holes and noise
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    
    # Keep only the largest connected component
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n > 1:
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        mask = np.where(labels == largest, 255, 0).astype(np.uint8)
    return mask


def _rotate_pts(pts, M):
    """
    Applies a 2D affine transformation to a set of points.

    Args:
        pts: Array of 2D points with shape (N, 2).
        M: 2×3 affine transformation matrix.

    Returns:
        Transformed points as an array of shape (N, 2).
    """

    # Convert points to homogeneous coordinates
    ones = np.ones((len(pts), 1), dtype=np.float32)
    h = np.hstack([pts.astype(np.float32), ones])

    # Apply the affine transformation
    return (M @ h.T).T


def _fit_line(pts):
    """
    Fits a straight line through a set of 2D points.

    The line comes back as a point on it plus a direction vector.

    Args:
        pts: Array of 2D points.

    Returns:
        Tuple (x0, y0, vx, vy), where (x0, y0) is a point on the fitted line and (vx, vy) is the line's direction vector.
    """

    # Fit a line through the supplied points
    vx, vy, x0, y0 = cv2.fitLine(
        pts.astype(np.float32),
        cv2.DIST_L2,
        0,
        0.01,
        0.01
    ).flatten()

    return (float(x0), float(y0), float(vx), float(vy))


def _intersect(l1, l2):
    """
    Finds where two lines cross.

    Parallel lines never cross, so those give None.

    Args:
        l1: First line represented as (x0, y0, vx, vy).
        l2: Second line represented as (x0, y0, vx, vy).

    Returns:
        Intersection point as a NumPy array of shape (2,), or None if the lines do not intersect uniquely.
    """

    # Unpack line parameters
    x1, y1, vx1, vy1 = l1
    x2, y2, vx2, vy2 = l2

    # Construct the linear system describing the intersection
    A = np.array([[vx1, -vx2], [vy1, -vy2]], dtype=np.float64)
    b = np.array([x2 - x1, y2 - y1], dtype=np.float64)

    # Solve for the intersection parameters
    try:
        t, _ = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        return None

    # Compute the intersection point
    return np.array([x1 + t * vx1, y1 + t * vy1], dtype=np.float32)


def find_quad(mask):
    """
    Finds the four corners of the card in the stencil.

    Builder Cards have rounded corners, so the corners are where straight lines fitted along the four edges cross, 
    which lands slightly outside the card itself. 
    If that fails it falls back to polygon approximation, and then to the smallest rectangle around the card.

    Args:
        mask: Binary foreground mask of the card.

    Returns:
        A (4, 2) array containing the detected corner coordinates, or None if no contour is found.
    """

    # Find all external contours in the binary mask
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None

    # Select the largest contour, assumed to be the card
    c = max(contours, key=cv2.contourArea)
    pts = c.reshape(-1, 2).astype(np.float32)

    # Rotate the contour so the card becomes approximately axis-aligned
    (cx, cy), (rw, rh), ang = cv2.minAreaRect(c)
    M = cv2.getRotationMatrix2D((cx, cy), ang, 1.0)
    P = _rotate_pts(pts, M)

    # Compute the bounding box of the rotated contour
    minx, maxx = P[:, 0].min(), P[:, 0].max()
    miny, maxy = P[:, 1].min(), P[:, 1].max()
    W, H = maxx - minx, maxy - miny
    bx, by = 0.18 * W, 0.18 * H

    # Define inner margins used for selecting edge points
    cxlo, cxhi = minx + bx, maxx - bx
    cylo, cyhi = miny + by, maxy - by

    # Split contour points into the four card edges
    top    = P[(P[:, 1] < miny + by) & (P[:, 0] > cxlo) & (P[:, 0] < cxhi)]
    bottom = P[(P[:, 1] > maxy - by) & (P[:, 0] > cxlo) & (P[:, 0] < cxhi)]
    left   = P[(P[:, 0] < minx + bx) & (P[:, 1] > cylo) & (P[:, 1] < cyhi)]
    right  = P[(P[:, 0] > maxx - bx) & (P[:, 1] > cylo) & (P[:, 1] < cyhi)]

    # Fit a line to each edge and compute their intersections
    if min(len(top), len(bottom), len(left), len(right)) >= 15:
        lt, lb = _fit_line(top), _fit_line(bottom)
        ll, lr = _fit_line(left), _fit_line(right)
        tl, tr = _intersect(lt, ll), _intersect(lt, lr)
        br, bl = _intersect(lb, lr), _intersect(lb, ll)
        if all(p is not None for p in (tl, tr, br, bl)):
            corners_rot = np.array([tl, tr, br, bl], dtype=np.float32)
            Minv = cv2.invertAffineTransform(M)
            return _rotate_pts(corners_rot, Minv).astype(np.float32)

    # Fall back to polygon approximation if line fitting fails
    peri = cv2.arcLength(c, True)
    for f in [0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10]:
        approx = cv2.approxPolyDP(c, f * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            return approx.reshape(4, 2).astype(np.float32)

    # Final fallback: return the minimum-area bounding rectangle
    return cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)


def order_corners(pts):
    """
    Orders four corners clockwise, starting top-left.

    Everything downstream assumes that order: top-left, top-right, bottom-right, bottom-left.

    Args:
        pts: Array containing four unordered corner points.

    Returns:
        A (4, 2) array with consistently ordered corner coordinates.
    """

    # Compute the sum and difference of each point's coordinates
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).reshape(-1)

    # Identify the four corners using coordinate relationships
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmin(d)]
    bl = pts[np.argmax(d)]

    # Return the corners in clockwise order
    return np.float32([tl, tr, br, bl])


def rectify(img_bgr, quad, mask, background="white"):
    """
    Straightens the card into a flat rectangle and cuts the background away.

    The stencil is warped through the same transform as the photo, so it still lines up pixel for pixel
    and is then used to fill or make transparent everything outside the card. 
    It is eroded by one pixel first, which removes the thin halo of table colour along the edge.

    Args:
        img_bgr: Original OpenCV image.
        quad: Four detected card corners.
        mask: Binary foreground mask.
        background: Output background mode ("white", "black", or "none").

    Returns:
        The rectified card image, or None if the computed output size is invalid.
    """

    # Order the detected corners consistently
    src = order_corners(quad)
    (tl, tr, br, bl) = src

    # Compute the dimensions of the rectified image
    w = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    h = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    if w < 2 or h < 2:
        return None

    # Compute the perspective transformation
    dst = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
    M = cv2.getPerspectiveTransform(src, dst)

    # Warp the original image
    warped = cv2.warpPerspective(img_bgr, M, (w, h))

    # Warp and refine the segmentation mask
    warped_mask = cv2.warpPerspective(mask, M, (w, h), flags=cv2.INTER_NEAREST)
    _, warped_mask = cv2.threshold(warped_mask, 127, 255, cv2.THRESH_BINARY)
    er = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    warped_mask = cv2.erode(warped_mask, er, iterations=1)

    # Return an RGBA image with a transparent background
    if background == "none":
        out = cv2.cvtColor(warped, cv2.COLOR_BGR2BGRA)
        out[:, :, 3] = warped_mask
        return out

    # Replace the background with the selected solid color
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
    """
    Downscales an image so its longest side does not exceed max_dim.

    Smaller images are returned untouched; nothing is ever upscaled.

    Args:
        img_bgr: OpenCV image.
        max_dim: Maximum allowed width or height.

    Returns:
        The original or resized OpenCV image.
    """

    # Get the image dimensions
    h, w = img_bgr.shape[:2]
    longest = max(h, w)

    # Skip resizing if the image already fits within the limit
    if longest <= max_dim:
        return img_bgr

    # Compute the scaling factor
    scale = max_dim / float(longest)

    # Resize while preserving the aspect ratio
    return cv2.resize(
        img_bgr,
        (int(round(w * scale)), int(round(h * scale))),
        interpolation=cv2.INTER_AREA,
    )


def draw_corners(img_bgr, quad):
    """
    Draws the detected outline and corners onto the photo.

    This is the visual check: when a card comes out cropped wrong, the overlay shows straight
    away whether the corners were found in the wrong place.

    Args:
        img_bgr: Original OpenCV image.
        quad: Four detected card corners.

    Returns:
        A copy of the input image with the detected quadrilateral and corner points drawn.
    """

    # Create a copy to avoid modifying the original image
    vis = img_bgr.copy()

    # Order the detected corners consistently
    pts = order_corners(quad).astype(int)

    # Draw the detected card outline - red lines
    cv2.polylines(vis, [pts.reshape(-1, 1, 2)], True, (0, 0, 255), 3)

    # Draw the green detected corner points
    for (x, y) in pts:
        cv2.circle(vis, (int(x), int(y)), 8, (0, 255, 0), -1)

    return vis


def encode_corners(img_bgr, quad):
    """
    Encodes the corners overlay as PNG bytes.

    Args:
        img_bgr: Original OpenCV image.
        quad: Four detected card corners.

    Returns:
        PNG-encoded image bytes, or None if encoding fails.
    """
    # Encode the corner visualization as a PNG image
    ok, buf = cv2.imencode(".png", draw_corners(img_bgr, quad))
    return buf.tobytes() if ok else None


def _decode_bgr(raw_bytes):
    """
    Decodes an uploaded image into an OpenCV BGR image.

    OpenCV handles the common formats but not HEIC, so those fall back to Pillow. 
    Only an upload that both of them refuse comes back as None.

    Args:
        raw_bytes: Encoded image bytes.

    Returns:
        OpenCV image in BGR format, or None if decoding fails.
    """

    # Attempt decoding using OpenCV
    img = cv2.imdecode(np.frombuffer(raw_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is not None:
        return img

    # Fall back to Pillow for unsupported image formats (HEIC/HEIF)
    try:
        pil = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        return cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
    except Exception as e:
        logger.warning("[ImageProcessor] Pillow fallback decode failed: %s", e)
        return None


def process_image(raw_bytes):
    """
    Runs the whole cleanup: decode, downscale, segment, find the corners and straighten.

    If no card is found the pipeline stops here and returns nothing, 
    which is how an upload can quietly end up with no finished image.

    Args:
        raw_bytes: Encoded input image.

    Returns:
        A tuple containing:
            - Rectified PNG image bytes (or None if no card was extracted).
            - Corner-overlay PNG image bytes (or None if no card was detected).
    """

    # Decode the uploaded image
    img = _decode_bgr(raw_bytes)
    if img is None:
        raise ValueError("could not decode image bytes")

    # Downscale large images to reduce processing cost
    h0, w0 = img.shape[:2]
    img = downscale(img, MAX_DIM)
    h1, w1 = img.shape[:2]
    if (w1, h1) != (w0, h0):
        logger.info("[ImageProcessor] downscaled %dx%d -> %dx%d (MAX_DIM=%d)", w0, h0, w1, h1, MAX_DIM)
    else:
        logger.info("[ImageProcessor] input %dx%d (<= MAX_DIM=%d, left as-is)", w0, h0, MAX_DIM)

    # The model must see the same downscaled image, so the mask lines up with what gets warped
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise ValueError("PNG re-encode failed")
    work_bytes = buf.tobytes()

    # Generate the segmentation mask and detect the card corners
    mask = get_mask(work_bytes, SESSION)
    quad = find_quad(mask)
    if quad is None:
        return None, None

    # Drawn on the same image the corners were detected on
    corners_png = encode_corners(img, quad)

    # Apply perspective correction.
    out = rectify(img, quad, mask, BACKGROUND)
    if out is None:
        return None, corners_png

    # Encode the rectified image as PNG
    ok, buf = cv2.imencode(".png", out)
    if not ok:
        raise ValueError("PNG encode failed")
    return buf.tobytes(), corners_png


def finished_key(card_id):
    """
    Builds the S3 key for the finished card image.

    Named by cardId rather than the slug, so renaming a card never breaks its picture.

    Args:
        card_id: Unique identifier of the processed card.

    Returns:
        S3 object key for the rectified PNG image.
    """

    return f"{FINISHED_PREFIX}{card_id}.png"


def corners_key(raw_key):
    """
    Builds the S3 key for the corners overlay, next to the original upload.

    The handler skips any key ending in __corners.png, so writing this back into the raw prefix cannot re-trigger the pipeline.

    Args:
        raw_key: S3 object key of the uploaded image.

    Returns:
        S3 object key for the corner-overlay PNG image.
    """

    # Remove the original file extension while preserving the path
    base = raw_key.rsplit(".", 1)[0] if "." in raw_key else raw_key

    # Append the corner-overlay suffix.
    return f"{base}__corners.png"

# Card-text extraction and metadata
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
The title is the card name printed at the top of the card.
Examples: "AWS Cloud Practitioner", "David".
If you cannot read it, use "".

TASK 2: EFFECT
The effect is the gameplay area in the middle of the card.

The effect area may contain small icons.
A card can have 1 icon, 2 icons, or 3 icons.
Not all icon types are always present.
Only use icons that are actually visible on this card.

There are exactly 3 possible icon types:

ICON TYPE 1: CREDIT
Visual appearance: orange circle contains a white number. That number has NO plus sign.
Example: orange circle with "1"
This icon can be small, so look carefully.
Meaning: "Get N credit", if N = 1.

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
Join multiple effect sentences using \n inside the JSON string.

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
    """
    Parses the JSON the vision model returned.

    The prompt asks for bare JSON and the model wraps it in a code fence anyway, 
    so the fence is stripped and everything outside the outermost braces is cut.

    Args:
        text: Raw text response produced by the Bedrock vision model.

    Returns:
        Dictionary containing the extracted title, effect, and description.
    """

    # Remove optional Markdown code fences
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]

    # Extract the JSON object from the response
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j != -1:
        t = t[i:j + 1]

    # Parse the JSON and normalize the expected fields
    data = json.loads(t)
    return {
        "title": str(data.get("title", "") or ""),
        "effect": str(data.get("effect", "") or ""),
        "description": str(data.get("description", "") or ""),
    }


def extract_card_fields(png_bytes):
    """
    Reads the card's title, effect and description with a Bedrock vision model.

    It is given the straightened image, not the original photo, and the temperature is 0 because this is extraction and not writing.

    Args:
        png_bytes: PNG-encoded rectified card image.

    Returns:
        Dictionary containing the extracted title, effect, and description.
    """

    # Invoke the Bedrock vision model
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

    # Parse the model response
    text = resp["output"]["message"]["content"][0]["text"]
    return _parse_json(text)


def parse_meta(raw_key):
    """
    Reads the event, year and card id back out of the upload's filename.

    The upload lambda builds that name as <event>_<year>_<cardId>
    and strips underscores out of the event, so splitting on "_" here stays correct.

    Args:
        raw_key: S3 object key of the uploaded image.

    Returns:
        Tuple containing (event, year, card_id).
    """

    # Extract the filename without its extension
    filename = raw_key.split("/")[-1]
    stem = filename.rsplit(".", 1)[0]

    # Split the filename into its metadata components
    parts = stem.split("_")
    if len(parts) >= 3:
        card_id = parts[-1]
        year = parts[-2]
        event = " ".join(parts[:-2]).replace("-", " ")
    else:
        card_id, year, event = stem, "", ""

    return event, year, card_id


def slugify(text):
    """
    Converts a string into a URL-friendly slug.

    Lowercase, with anything non-alphanumeric turned into a single hyphen, 
    so f.e. "AWS Summit Hong Kong 2025" becomes "aws-summit-hong-kong-2025".

    Args:
        text: Input string.

    Returns:
        URL-friendly slug.
    """

    # Normalize the input string
    s = (text or "").strip().lower()

    # Replace non-alphanumeric characters with hyphens
    s = re.sub(r"[^a-z0-9]+", "-", s)

    # Remove leading and trailing hyphens
    return s.strip("-")


def write_card_item(card_id, event, year, raw_key, fin_key, fields, uploader=""):
    """
    Creates the card's pending record in DynamoDB.

    Every model-written value lands in its own *_ai_draft field and the approved fields are written empty, 
    so the model has no way to write a value that gets published.

    Args:
        card_id: Unique identifier of the card.
        event: Event where the card was obtained.
        year: Event year.
        raw_key: S3 key of the uploaded image.
        fin_key: S3 key of the rectified card image.
        fields: Dictionary containing AI-generated card fields.
        uploader: Optional uploader attribution stored in the S3 object metadata.

    Returns:
        None. The card record is written to DynamoDB as a side effect, where it waits with
        status "pending" until a human approves it.
    """

    # Generate a timestamp for the new record
    now = datetime.now(timezone.utc).isoformat()

    # The year is only appended when the title does not already carry it
    title_draft = fields.get("title", "")
    year_str = str(year).strip()
    slug_src = f"{title_draft} {year_str}" if year_str and year_str not in title_draft else title_draft
    slug_draft = slugify(slug_src)

    # Store the pending card record in DynamoDB
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
    """
    Sends the email saying a new card is waiting to be reviewed.

    It carries the card's metadata, CLI commands for pulling the three images down, and the link to the review editor. 
    Sending is best effort, so a failed email cannot fail the pipeline.

    Args:
        bucket: S3 bucket containing the uploaded images.
        finished_key: S3 key of the rectified card image.
        card_id: Unique identifier of the processed card.
        event_name: Event where the card was obtained.
        year: Event year.
        title_draft: AI-generated draft title.
        raw_key: S3 key of the original uploaded image.
        event_time: Optional upload timestamp.

    Returns:
        None. The notification is published as a side effect; the function returns early without
        publishing when no SNS topic is configured.
    """

    # Skip notification if no SNS topic has been configured
    if not SNS_TOPIC_ARN:
        logger.info("[ImageProcessor] SNS_TOPIC_ARN not set, skipping notification")
        return

    # Build the download command for the rectified card image
    finished_cmd = (
        "# 3) Cropped / finished card -> Hugo static/cards\n"
        f"cd {STATIC_CARDS_DIR}\n"
        f"aws s3 cp s3://{bucket}/{finished_key} {card_id}.png"
    )

    # Build download commands for the original image and corner visualization
    if raw_key:
        raw_name = raw_key.split("/")[-1]
        base = raw_key.rsplit(".", 1)[0] if "." in raw_key else raw_key
        corners_obj_key = base + "__corners.png"
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

    # Compose the notification email
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

    # Publish the SNS notification
    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=f"New Builder Card: {event_name or 'Unknown'} ({year or 'Unknown'})",
            Message=message,
        )
        logger.info("[ImageProcessor] SNS notification sent for %s", finished_key)
    except Exception as e:
        logger.exception("[ImageProcessor] failed to send SNS notification: %s", e)


def handler(event, context):
    """
    Lambda entry point: cleans up each uploaded image, then drafts and records its text.

    The database record is written before the finished image, so nothing downstream can race anitem that does not exist yet.
    A record that raises is logged and the rest still run.

    Args:
        event: S3 event notification; only its "Records" list is read.
        context: Lambda context object, unused.

    Returns:
        Dict with statusCode 200 and a JSON body listing one result per record.
    """

    # Log the configured processing settings
    logger.info("[ImageProcessor] model=%s background=%s", MODEL, BACKGROUND)
    results = []

    # Process each S3 event record independently
    for record in event.get("Records", []):
        s3_info = record.get("s3", {})
        bucket = s3_info.get("bucket", {}).get("name")
        key = s3_info.get("object", {}).get("key")
        if not bucket or not key:
            logger.info("[ImageProcessor] missing bucket/key, skipping")
            continue

        # Decode URL-escaped S3 object keys
        key = urllib.parse.unquote_plus(key)

        # Skip non-image objects and generated helper images
        if key.endswith(".keep"):
            logger.info("[ImageProcessor] skipping .keep placeholder")
            continue
        if key.endswith("__corners.png"):
            logger.info("[ImageProcessor] skipping corners-overlay image (not a card)")
            continue
        if not key.startswith(RAW_PREFIX):
            logger.info("[ImageProcessor] key not under RAW_PREFIX=%s, skipping", RAW_PREFIX)
            continue

        # Download and process the uploaded image
        logger.info("[ImageProcessor] processing s3://%s/%s", bucket, key)
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            raw = obj["Body"].read()

            # The uploader's name rides along as object metadata, set by the signed upload
            uploader = obj.get("Metadata", {}).get("uploader", "")

            # Detect and straighten the card
            png, corners_png = process_image(raw)
            if png is None:
                logger.warning("[ImageProcessor] no card detected in %s", key)
                results.append({"key": key, "status": "no_card"})
                continue

            # Keep the corners overlay next to the original, as a visual check
            if corners_png is not None:
                ck = corners_key(key)
                try:
                    s3.put_object(Bucket=bucket, Key=ck, Body=corners_png, ContentType="image/png")
                    logger.info("[ImageProcessor] wrote corners overlay s3://%s/%s", bucket, ck)
                except Exception as ex:
                    logger.exception("[ImageProcessor] corners write failed: %s", ex)

            # Read the metadata first, so the finished image can be named by cardId
            event_name, year, card_id = parse_meta(key)
            out_key = finished_key(card_id)

            # Both of these are best effort and neither is allowed to fail the pipeline
            fields = {"title": "", "effect": "", "description": ""}
            try:
                fields = extract_card_fields(png)
                logger.info("[ImageProcessor] extracted title=%r", fields.get("title", ""))
            except Exception as ex:
                logger.exception("[ImageProcessor] Bedrock extraction failed: %s", ex)
            try:
                write_card_item(card_id, event_name, year, key, out_key, fields, uploader)
                logger.info("[ImageProcessor] wrote DynamoDB item cardId=%s (pending)", card_id)
            except Exception as ex:
                logger.exception("[ImageProcessor] DynamoDB write failed: %s", ex)

            # Write the finished image, after the record that points at it
            s3.put_object(
                Bucket=bucket,
                Key=out_key,
                Body=png,
                ContentType="image/png",
            )
            logger.info("[ImageProcessor] wrote s3://%s/%s", bucket, out_key)

            # Tell me there is a card to review
            publish_upload_notification(
                bucket, out_key, card_id, event_name, year,
                fields.get("title", ""), key, record.get("eventTime", ""),
            )

            results.append({"key": key, "finished": out_key, "status": "ok"})
        except Exception as e:
            # Continue processing remaining records even if one fails
            logger.exception("[ImageProcessor] failed on %s: %s", key, e)
            results.append({"key": key, "status": "error", "error": str(e)})

    # Return the processing summary
    return {"statusCode": 200, "body": json.dumps({"results": results})}
