<!-- SPDX-License-Identifier: MIT
Copyright (c) 2026 Michal Salanci -->

# Image-Processor Lambda (container)

The expensive middle of the pipeline, deployed as `BuilderCardsImageProcessor`. The gate has
already decided the upload is (probably) an AWS Builder Card; this function straightens the
crooked photo into a clean PNG, has a **Bedrock** vision model draft the card's text, writes
the **DynamoDB** record the review editor works from, and sends the "go review it" email.
Everything it writes is a *draft* (section 2). Stack-wide setup and deploy:
[repo root README](../../../README.md).

- **Trigger:** the gate **async-invokes** it (`InvocationType="Event"`), passing the S3 event
  record through unmodified — which is why `test-event.json` is S3-shaped. There is **no S3
  trigger on this function**: card-detect owns the stack's only S3 notification and forwards
  only what scored as a card.
- **Runtime:** `public.ecr.aws/lambda/python:3.14`, **arm64** (Graviton), CPU only — BiRefNet
  runs inside the container, with no GPU. Sized in `lambda.tf`: **10,240 MB**, **180 s**
  timeout, `reserved_concurrent_executions = 2`.
- **Weights:** BiRefNet-General-**Lite** (~224 MB) download at **Docker build** time into
  `/opt/models` (`U2NET_HOME`) — cold starts fetch nothing, and there is no manual step.
- **ONNX, to keep it straight:** this Lambda *runs* onnxruntime (`rembg[cpu]` pulls it in —
  exactly why the Dockerfile installs **`libgomp`**, the OpenMP runtime the base image lacks)
  but has **no ONNX export step**. `build_onnx.py` and `build_onnx-requirements.txt` belong
  to [card-detect](../card-detect/README.md) alone.

---

## 1. What one upload goes through

Records are processed independently. The handler skips `.keep` placeholders, keys outside
`RAW_PREFIX`, and anything ending `__corners.png` — its own debug output, which is what stops
a re-trigger loop.

### The image pipeline (`process_image`)

1. **Decode** (`_decode_bgr`) — OpenCV first; if it refuses (HEIC/HEIF from iPhones), Pillow
   with `pillow-heif`. Only an upload both refuse fails.
2. **Downscale** (`downscale`) — longest side capped at `MAX_DIM` (2048); nothing is
   upscaled. The downscaled image is re-encoded to PNG and *that* is what the model sees, so
   the mask lines up pixel-for-pixel with what gets warped.
3. **Segment** (`get_mask`) — rembg/BiRefNet returns an RGBA cutout; its alpha is thresholded
   at **127**, a 15×15 elliptical close/open kills pinholes and specks, and only the largest
   connected component survives. rembg min-max stretches every mask first, so 127 is the
   midpoint between the least and most card-like pixel *in that photo*, not 50% confidence.
4. **Find the corners** (`find_quad`) — Builder Cards have **rounded** corners, so the corners
   are not on the contour. It is rotated approximately axis-aligned (only then do "top" and
   "left" mean anything) and split into four edge runs **ignoring the outer 18% at each end**,
   where the rounding would bend the fit; a line is fitted to each (`cv2.fitLine`) and the four
   **intersections** are the corners, landing slightly *outside* the rounding. Each edge needs
   **≥ 15 points**; below that the fallbacks are `approxPolyDP` (7 epsilon steps), then the
   minimum-area rectangle. `0.18` and `15` are tuned by eye — a steeper angle wants a wider band.
5. **Straighten and crop** (`order_corners` + `rectify`) — one perspective matrix maps the quad
   onto a card-sized canvas, so both happen at once: there is no separate crop, the pixels
   outside the quad are simply never sampled. It is applied to **both** image and mask, so they
   stay aligned. The corners sit outside the card, so the warp drags four wedges of table along;
   the warped mask removes them. It is eroded by one pixel first (3×3 — kills the halo of table
   colour on the edge), then everything outside the card becomes `BACKGROUND`, or the alpha when
   `BACKGROUND=none`. The result is an OpenCV image in memory, not PNG bytes yet.
6. **Encode** — the card as PNG, plus a `draw_corners`/`encode_corners` **debug overlay** (the
   photo, outline in red, corners in green) written beside the original as `<stem>__corners.png`.

No card found ⇒ the record ends here with `"status": "no_card"`.

### Then: text draft, record, email

7. **Metadata from the filename** (`parse_meta`) — `get-upload-url` names objects
   `<event>_<year>_<uuid>.jpg` (spaces hyphenated, no underscores in the event), so
   event/year/cardId split back out on `_`. The finished PNG is written to
   `images/finished/<cardId>.png` — named by cardId, so renaming a card never breaks its
   picture. The uploader's name rides along as S3 object metadata.
8. **Bedrock extraction** (`extract_card_fields`) — the **straightened** PNG, not the original
   photo, goes to the vision model via `converse` at `temperature 0.0` / `maxTokens 1200`:
   extraction, not writing. The prompt is the constants `EXTRACT_SYSTEM` + `EXTRACT_PROMPT` —
   JSON only, exactly the keys `title`/`effect`/`description`, plus how to read the three
   effect-icon types (orange circle = credits, dark rounded rectangle = draw cards, white cloud
   = cloud-adoption effects) and how to emit links and bold as Markdown. `_parse_json` strips
   the code fence the model adds anyway. Prompt edits are `app.py` edits: container rebuild.
9. **DynamoDB record** (`write_card_item`) — one item, `status = "pending"`.
10. **SNS email** (`publish_upload_notification`) — to `upload_notifications`: metadata,
    paste-ready `aws s3 cp` commands for all three images, and `REVIEW_URL`.

**Ordering and failure policy:** the DynamoDB record is written **before** the finished image,
so nothing downstream can race an item that does not exist yet. Bedrock, DynamoDB and SNS are
best-effort — failures are logged and the pipeline continues (a failed Bedrock call just means
empty drafts); a record that raises entirely is logged and the rest still run. **If the whole
invocation dies** (crash, timeout, OOM kill) the async queue retries it, twice by default, and
then drops it — there is no DLQ, so the upload just sits in `images/raw/` with no record, no
email and nothing but CloudWatch to show for it.

```bash
aws logs tail /aws/lambda/BuilderCardsImageProcessor --follow
```

## 2. The AI-draft boundary

**Every value the Bedrock model produces is written to a `*_ai_draft` field, and the approved
fields are written empty.** `write_card_item` puts `title_ai_draft`, `effect_ai_draft`,
`description_ai_draft` and `slug_ai_draft` (slug derived from the draft title, year appended
only if the title lacks it), while `title`, `effect`, `description` and `slug` go in as `""`
for a human to fill via the review editor. `status = "pending"`, and the committer only ever
publishes `approved` — which it re-verifies itself. The model has no field it can write that
reaches the public site.

## 3. Files, environment, dependencies

| File               | What it does                                                                                                                                                                  |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app.py`           | Handler + the whole pipeline above. **Runs in production.**                                                                                                                    |
| `Dockerfile`       | arm64 image on `public.ecr.aws/lambda/python:3.14`; installs `libgomp`, pre-downloads the BiRefNet-lite weights into `/opt/models` and `chmod`s them (the Lambda is not root). Also sets `NUMBA_CACHE_DIR=/tmp` and `MPLCONFIGDIR=/tmp` — the filesystem is read-only except `/tmp`, and both libraries write caches at import. |
| `requirements.txt` | Pinned runtime deps (below). `boto3` comes from the base image.                                                                                                                |
| `test-event.json`  | Sample S3-shaped event for local invokes — replace the placeholder key first.                                                                                                  |
| `.dockerignore`    | Trims the build context: `__pycache__`, `*.pyc`, `.venv/`, `input/`, `output/`, `*.md`.                                                                                        |

Terraform lives one level up in `infrastructure/`: `ecr.tf` (`null_resource.image_processor_build`
— build + push), `lambda.tf` (function, sizing, env), `iam.tf` (read raw, write the overlay back
into raw, write finished, `dynamodb:PutItem`, `bedrock:InvokeModel`, SNS publish — no delete).

### Environment

`lambda.tf` sets everything below except `REMBG_MODEL` and `MAX_DIM`, which only ever take the
`app.py` default.

| Var                                                                | Default (`app.py`)                                                                  | Meaning                                                                                                          |
| ------------------------------------------------------------------ | ----------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `REMBG_MODEL`                                                      | `birefnet-general-lite`                                                             | Segmentation model. **Not a real knob** — see the warning in section 5.                                          |
| `MAX_DIM`                                                          | `2048`                                                                              | Longest side before downscaling.                                                                                 |
| `BACKGROUND`                                                       | `black` (same in `lambda.tf`)                                                       | Fill behind the card: `black`, `white` or `none` (transparent RGBA).                                             |
| `RAW_PREFIX`                                                       | `images/raw/`                                                                       | Input prefix; other keys are skipped.                                                                            |
| `FINISHED_PREFIX`                                                  | `images/finished/`                                                                  | Where the cleaned PNG goes.                                                                                      |
| `DDB_TABLE`                                                        | `BuilderCards`                                                                      | Table the pending record is written to.                                                                          |
| `BEDROCK_MODEL_ID`                                                 | *(unset)*                                                                           | Vision model; `lambda.tf` passes `var.bedrock_llm`. Unset ⇒ extraction fails soft and the drafts come back empty. |
| `BEDROCK_REGION`                                                   | `eu-central-1`                                                                      | Region the call goes to (`var.bedrock_region`) — the inference profile is region-specific.                        |
| `SNS_TOPIC_ARN`                                                    | *(empty)*                                                                           | The `upload_notifications` topic. Empty ⇒ no email sent.                                                          |
| `REVIEW_URL`                                                       | *(empty)*                                                                           | Review-editor link included in the email.                                                                        |
| `STATIC_CARDS_DIR`<br>`RAW_DOWNLOAD_DIR`<br>`CORNERS_DOWNLOAD_DIR` | `web/awsbuildercards/static/cards`<br>`card_pictures/RAW`<br>`card_pictures/CORNERS` | **Cosmetic.** They only compose the copy-paste commands *inside the email*. The first default points into the maintainer's separate Hugo site repo; nothing in this stack reads or writes any of them. |

### Dependencies (pinned, `requirements.txt`)

| Package                                 | Why                                                                                                                              |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `rembg[cpu]==2.0.78`                    | BiRefNet segmentation (CPU onnxruntime).                                                                                         |
| `opencv-python-headless==5.0.0.93`      | Corner detection, warping, PNG encoding.                                                                                         |
| `numpy==2.4.6`                          | **Do not bump past 2.4.x:** numba (pulled in by rembg via pymatting) caps numpy below 2.5. A "helpful" upgrade breaks resolution. |
| `pillow==12.3.0` + `pillow-heif==1.5.0` | Decode fallback, and HEIC/HEIF (iPhone) support.                                                                                 |

## 4. Memory: why 10 GB and why concurrency 2

This function runs at **10,240 MB — the largest memory size Lambda offers — and has still been
OOM-killed at that ceiling.** Nowhere bigger to go, so treat memory as the scarce resource.
Three things before you tune it:

- **The peak is a constant, not a function of the photo.** rembg resizes every image to
  1024×1024 before BiRefNet sees it, so a 0.3 MP thumbnail and a 3 MP photo cost the model the
  same. `MAX_DIM` bounds only what stands *around* the model (RGBA composite, mask upsample,
  OpenCV arrays, PNG re-encode) — real, but small next to the model's floor.
- **The kills track container reuse, not size.** In the author's logs no OOM kill was ever the
  *first* invocation in a container. `reserved_concurrent_executions = 2` caps how many
  maximum-size containers run at once — and forces a burst to reuse the same two.
- **The async retry is the safety net.** Most OOM'd uploads went through on a retry: that is
  the async invoke, not anything designed in.

Picking the smaller **Lite** model bought the real headroom; maxing the memory also buys the
most vCPU Lambda offers, which matters with no GPU under the container.

## 5. Build & deploy

`terraform apply` rebuilds and pushes the image (`ecr.tf` →
`null_resource.image_processor_build`) whenever the hash of `Dockerfile`, `app.py` or
`requirements.txt` changes — so a Python change here **needs the container rebuild**, while an
env-var-only change applies through Terraform alone. The build runs `docker buildx build
--platform linux/arm64 --provenance=false --load`, then `docker push`; a failed build deploys
nothing, because Terraform aborts before updating the Lambda image. macOS/Docker Desktop
failures: [root README → Deploy gotchas](../../../README.md#deploy-gotchas-container-builds).

> **Changing the segmentation model takes two edits, not one.** The weights are baked in by the
> `RUN python -c "... new_session('birefnet-general-lite')"` line in the `Dockerfile`;
> `REMBG_MODEL` only *names* a model. Change one without the other and you ship a container
> whose weights do not match its code — or a name that was never pre-downloaded, which means a
> fetch at cold start into a `/opt` that is read-only.

Cold starts do not finish inside Lambda's 10 s init window, so the model load lands inside the
first invocation — don't read the first `Duration` as typical. After an apply, "done" is
`Apply complete` plus a test upload whose `[ImageProcessor]` log lines end with `wrote
s3://.../images/finished/<cardId>.png`.

## 6. Run locally (Lambda Runtime Interface Emulator)

The AWS base image ships the RIE, so the container can be invoked locally with plain `curl`.
**It needs real AWS credentials** — the handler reads from S3, writes the PNG and the overlay
back to S3, writes DynamoDB, calls Bedrock and (if configured) publishes SNS. A local run is a
real run against the real bucket and the real table.

```bash
cd infrastructure/lambda/image-processor
# native on Apple Silicon; on x86 this builds and runs under emulation — slow, but it works
docker build --platform linux/arm64 -t buildercards-image-processor .

docker run --platform linux/arm64 -p 9000:8080 \
  -v ~/.aws:/root/.aws:ro \
  -e AWS_PROFILE=default \
  -e AWS_REGION=eu-central-1 \
  -e BEDROCK_MODEL_ID=<your bedrock inference profile> \
  buildercards-image-processor
```

Defaults, locally: `SNS_TOPIC_ARN` is empty, so **no email is sent**; leave `BEDROCK_MODEL_ID`
off and extraction fails soft with empty drafts (useful for testing just the image pipeline);
`DDB_TABLE` defaults to `BuilderCards`, so the write hits the **real table** unless you point it
elsewhere. Add `-e BACKGROUND=none` (or `white`) for a different background.

```bash
# in another terminal — first point test-event.json at a real object under images/raw/
curl -s "http://localhost:9000/2015-03-31/functions/function/invocations" \
  -d @test-event.json | jq          # expect "status": "ok" with a "finished" key

aws s3 cp "s3://<bucket>/images/finished/<cardId>.png" /tmp/out.png
aws s3 cp "s3://<bucket>/images/raw/<stem>__corners.png" /tmp/corners.png
open /tmp/out.png /tmp/corners.png
```

Fastest bad-crop debug: green corner dots in the wrong place mean `find_quad`, not the warp.
