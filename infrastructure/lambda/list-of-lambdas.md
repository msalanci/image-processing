<!--
SPDX-License-Identifier: MIT
Copyright (c) 2026 Michal Salanci
-->

# BuilderCards Lambdas

Reference for every Lambda in the stack: what it does, how it's triggered, how
exposed it is, what permissions it holds, and how the pieces connect. There are
**5 active Lambdas** (get-upload-url → **card-detect gate** → image-processor →
review-editor → **committer**, where the review-editor invokes the committer to
publish OR delete a card) plus **1 retired** (`upload-notifier`, merged into the
image-processor — see the end), so the `lambda/` history may show extra names.
The `committer` is itself the **MERGE of the former separate `publisher` +
`deleter`** Lambdas (same PAT, both invoke-only, both called only by the
review-editor); their source is in `X-TRASH/publisher/` and `X-TRASH/deleter/`, and
their removed Terraform is recorded word for word in `HOW_WE_BUILD.md`.

All functions run in **eu-central-1**. Terraform defines them in
`infrastructure/lambda.tf` (functions), `infrastructure/iam.tf` (roles/policies),
`infrastructure/apigw.tf` (HTTP APIs), `infrastructure/s3.tf` (S3 triggers),
`infrastructure/sns.tf` (topics), `infrastructure/eventbridge.tf` (Amplify
deploy-event rule -> SNS3) and `infrastructure/secrets.tf` (secret containers).

---

## End-to-end flow

```
                 ┌─────────────────┐
  upload form ──▶│ get-upload-url  │  (PUBLIC API GW, no auth)
                 └────────┬────────┘
                          │ pre-signed PUT
                          ▼
                 s3://images/raw/<event>_<year>_<uuid>.jpg
                          │  (S3 ObjectCreated:Put, *.jpg)
                          ▼
                 ┌─────────────────┐
                 │ card-detect gate│  (INTERNAL, container, ~2 GB)
                 │  - ONNX ViT-B/32 P(card)
                 │  - card/unsure -> async-invoke image-processor
                 │  - junk        -> move to images/rejected/ + SNS4 email
                 └────────┬────────┘
                          │ lambda.invoke (Event, same S3 event)  [card/unsure only]
                          ▼
                 ┌─────────────────┐
                 │ image-processor │  (INTERNAL, container)
                 │  - rembg/OpenCV clean + straighten
                 │  - Bedrock Nova vision -> title/effect/description drafts
                 │  - write DynamoDB item (status=pending)
                 │  - write finished PNG + corners overlay to S3
                 │  - publish SNS1 "new card, go review"   ◀── (merged-in notifier)
                 └────────┬────────┘
                          │ SNS1 email (review URL)
                          ▼
                   you open the review editor
                 ┌─────────────────┐
                 │ review-editor   │  (INTERNET, Basic Auth)
                 │  - list / edit / approve cards in DynamoDB
                 │  - on approve: invoke committer {action:publish}
                 │  - on delete : invoke committer {action:delete}
                 └────────┬────────┘
                          │ lambda.invoke (RequestResponse) {action, cardId}
                          ▼
                 ┌─────────────────┐
                 │ committer       │  (INTERNAL, holds the GitHub PAT)
                 │  (merged publisher + deleter)
                 │  - publish: commit <slug>.md + <cardId>.png directly to master
                 │  - delete : un-publish GitHub files + S3 objects + DynamoDB item
                 └─────────────────┘
                          │ publish push lands on master (the deploy branch)
                          ▼
                 ┌─────────────────┐
                 │ AWS Amplify     │  (NOT Terraform-managed; console app)
                 │  - builds the Hugo site (amplify.yml) + deploys
                 └────────┬────────┘
                          │ "Amplify Deployment Status Change" event
                          ▼
                 EventBridge rule (SUCCEED/FAILED) ──▶ SNS3 email
                                                       "deploy SUCCEEDED / FAILED"
```

## Exposure tiers (least-privilege summary)

| Lambda          | Exposure                     | Holds secret? | Worst thing a compromise could do                                                                 |
| --------------- | ---------------------------- | ------------- | ------------------------------------------------------------------------------------------------- |
| get-upload-url  | **Public** (unauthenticated) | no            | Write objects under `images/raw/*` only                                                           |
| review-editor   | **Internet** (Basic Auth)    | basic-auth    | Edit the DynamoDB catalog (no delete); read/delete `rejected/*` + write `raw/*`; invoke committer |
| card-detect     | Internal (S3 event)          | no            | Read `raw/*`, move to `rejected/*`, invoke the image-processor, SNS4                              |
| image-processor | Internal (gate invoke)       | no            | Write S3 raw/finished, write DynamoDB, invoke Bedrock, publish SNS1                               |
| committer       | Internal (invoke only)       | github-pat    | Publish (commit to master) OR delete a card's DynamoDB item + S3 objects + published GitHub files |

Design rule followed: **split on different permissions or exposure.** The GitHub
PAT (the only credential that can change the public site) lives solely in the
internal `committer`, never in an internet-facing Lambda. (Publisher + deleter
were merged into the committer — they shared the same PAT, exposure, and caller,
so keeping them split bought nothing.)

---

## 1. get-upload-url

|                   |                                                                       |
| ----------------- | --------------------------------------------------------------------- |
| **Function name** | `BuilderCardsGetUploadUrl`                                            |
| **Source**        | `lambda/get-upload-url/index.py`                                      |
| **Packaging**     | Zip, Python 3.14                                                      |
| **Trigger**       | API Gateway HTTP API `BuilderCardsUploadApi` — `POST /get-upload-url` |
| **Exposure**      | **Public, unauthenticated** (CORS `*`). The only truly open surface.  |
| **IAM role**      | `BuilderCardsGetUploadUrlRole`                                        |

**Purpose.** Hands the browser a short-lived (5 min) pre-signed S3 **POST** (url +
signed form fields) so the upload form can upload a card photo straight to S3
without AWS credentials.

<!-- was (kept for rollback): "pre-signed S3 **PUT** URL ... Signs a `put_object` URL
     with `ContentType` + `Metadata={uploader}` (so the browser PUT must echo the
     `x-amz-meta-uploader` header)."
     STALE since the switch to generate_presigned_post: a pre-signed PUT cannot bound
     the body size, so the 25 MiB cap was unenforceable. See PROBLEMS.md #4. -->

**Behavior.**
- Accepts `{contentType, eventName, year, uploader}`.
- Builds the object key `images/raw/<event>_<year>_<uuid>.jpg`.
- Signs a `generate_presigned_post` policy pinning the exact key, the exact
  `Content-Type`, the exact `x-amz-meta-uploader` value and
  `["content-length-range", 1, MAX_UPLOAD_BYTES]` - the last of which is the only
  reason the 25 MiB cap is enforceable at all. The browser must send the returned
  fields verbatim. No tagging is signed.

**Validation (rejected with 400 before any URL is issued).**
- **`contentType` allowlist**: `image/jpeg`, `image/jpg`, `image/png`,
  `image/webp`, `image/heic`, `image/heif`, **`image/bmp`, `image/tiff`**. NOTE:
  `bmp`/`tiff` work end-to-end (Pillow + OpenCV decode them natively). `heic/heif`
  now decode end-to-end too: `pillow-heif` (1.5.0) is baked into BOTH the gate and
  image-processor containers — the gate calls `register_heif_opener()` so
  `Image.open` handles HEIC, and the processor adds a Pillow(+HEIF) fallback in
  `_decode_bgr()` since `cv2.imdecode` has no HEIC codec (iPhone photos are HEIC
  by default). **AVIF is intentionally excluded** (needs extra native decode libs
  in both containers). The object is always stored with a `.jpg` key regardless of
  the declared type.
- **`year`**: must be 4 digits within **`[2020, current year]`**, where the upper
  bound is computed per-request from the clock (`datetime.now(timezone.utc).year`)
  so it rolls over automatically each Jan 1 — rejects future-dated and pre-2020.
- **`eventName`** ≤ 100 chars, **`uploader`** ≤ 60 chars; the event is sanitised
  into the key (`[^A-Za-z0-9-]+ -> -`, capped 60, fallback `card`).

**Environment.** `BUCKET_NAME`, `RAW_PREFIX`.

**Permissions.** `s3:PutObject` on `images/raw/*` only.

**Notes.** Already hardened (PROBLEMS.md #4): an API-stage throttle (5 rps /
burst 10 in `apigw.tf`), input validation + key sanitisation in the Lambda, and
reserved concurrency on the downstream image-processor. Still the most exposed
surface, so the one to watch first if abuse ever shows up.

---

## 2. card-detect (gate)

|                   |                                                            |
| ----------------- | ---------------------------------------------------------- |
| **Function name** | `BuilderCardsCardDetect`                                   |
| **Source**        | `lambda/card-detect/index.py`                              |
| **Packaging**     | **Container image** (ECR), arm64, 2048 MB, 60 s timeout    |
| **Trigger**       | S3 `ObjectCreated:Put` on `images/raw/` with suffix `.jpg` |
| **Exposure**      | Internal (S3 event only)                                   |
| **IAM role**      | `BuilderCardsCardDetectRole`                               |

**Purpose.** A cheap "is this actually a card?" gate in FRONT of the expensive
image-processor. It TOOK OVER the `images/raw/*.jpg` S3 trigger; obvious junk is
quarantined before any BiRefNet/Bedrock cost is spent, while anything remotely
card-like still flows through to review exactly as before.

**Why separate (not baked into the image-processor).** The processor already
peaks ~7.5 GB and OOMs at its 10 GB ceiling on a real fraction of uploads; a
resident ONNX model in that container would worsen those OOMs. So the gate is an
isolated ~2 GB function and the processor keeps its full 10 GB.

**Behavior (per uploaded `.jpg`).**
1. Reads the raw bytes, computes `P(card)` with a proven ONNX ViT-B/32 (laion2b)
   classifier (the exact parity-checked artifacts from `local-tests/card-detect/`,
   baked into the image; torch-free — onnxruntime + numpy + pillow).
2. **Abstain band (fails TOWARD processing):**
   - `P >= KEEP_THRESHOLD` (0.50) → **card** → async `lambda.invoke` the
     image-processor with the *same* S3 event.
   - `DROP..KEEP` → **unsure** → also forwarded (still lands in review).
   - `P < DROP_THRESHOLD` (0.20) → **junk** → move the raw object to
     `images/rejected/` (copy + delete) and publish **SNS4** ("rejected"). No
     processing, no DynamoDB, no BiRefNet.
3. Structured log line `{key, p_card, verdict, action}` on every invocation.

Only obvious junk (measured `<= 0.03`) is auto-dropped, so a real card is never
silently killed by the gate. The forward is **async** (`Event`), so the gate
returns immediately (doesn't wait for the 60-180 s processor run) and gets
Lambda's built-in async retries — same fire-and-forget behaviour S3 gave before.

**Note on reprocess (force-process).** The gate's ONLY involvement in the
rejected-quarantine feature is a small check in its scoring path: if an incoming
raw object carries the `force-process` object-metadata marker (set by the review
editor's "put to process" when it copies a rejected image back into `raw/`), the
gate SKIPS scoring and forwards it straight to the image-processor — so a false
negative isn't re-scored and re-rejected in a loop. All the list / preview /
delete / copy-back S3 work is done by the **review editor** itself (see its
section); the gate holds no rejected-management powers and is not invoked by the
editor.

**Environment.** `RAW_PREFIX`, `REJECTED_PREFIX`, `IMAGE_PROCESSOR_FUNCTION_NAME`,
`REJECT_TOPIC_ARN` (SNS4), `KEEP_THRESHOLD`, `DROP_THRESHOLD`, `MODEL_DIR`
(`/opt/model`, where the baked `visual.onnx` + `text.npz` live).

**Permissions.** `s3:GetObject` on `raw/*`; `s3:PutObject` on `rejected/*` +
`s3:DeleteObject` on `raw/*` (the quarantine move; soft delete on the versioned
bucket); `lambda:InvokeFunction` on the image-processor ONLY; `sns:Publish` on
the SNS4 reject topic ONLY. **No Bedrock, no DynamoDB, no GitHub, no SNS1.**

**⚠ Deploy note.** Container Lambda, same auto-build pattern as the
image-processor: `terraform apply` rebuilds + pushes the arm64 image (the
`null_resource` in `ecr.tf`) when `Dockerfile`/`index.py`/`requirements.txt` or
the baked model artifacts change. The build **stages the proven `onnx_out/`**
from `local-tests/card-detect/onnx_out/` into the build context (so the exact
verified bytes are shipped — no re-export, no parity drift) and bakes them into
the image. Requires Docker + buildx on the machine running `apply`.

---

## 3. image-processor

|                   |                                                           |
| ----------------- | --------------------------------------------------------- |
| **Function name** | `BuilderCardsImageProcessor`                              |
| **Source**        | `lambda/image-processor/app.py`                           |
| **Packaging**     | **Container image** (ECR), arm64, 10240 MB, 180 s timeout |
| **Trigger**       | Async `lambda.invoke` (Event) from the card-detect gate   |
| **Exposure**      | Internal (gate invoke only)                               |
| **IAM role**      | `BuilderCardsImageProcessorRole`                          |

**Purpose.** The heavy worker. Turns a raw card photo into a clean, straightened
card image, drafts the card's text with a vision model, records the card in
DynamoDB as `pending`, and emails you to come review it.

**Behavior (per uploaded `.jpg`).**
1. Background removal (rembg / BiRefNet-lite) + OpenCV quad detection, perspective
   straighten, crop, background fill (default black).
2. Writes a corners-overlay PNG back to `images/raw/<stem>__corners.png` (a visual
   trust-check). The `__corners.png` suffix + the `.jpg` trigger filter prevent a
   re-trigger loop.
3. Calls **Bedrock** (vision, `var.bedrock_llm`) to draft `title` / `effect` /
   `description`; derives a `slug` draft. These are stored as `*_ai_draft` fields.
4. Writes the DynamoDB item (`status=pending`, approved fields empty, drafts set).
   **DB item is written before the finished PNG** so downstream consumers never
   race a missing item.
5. Writes the finished PNG to `images/finished/<cardId>.png`.
6. **Publishes SNS1** ("new card uploaded, go review it", with the review-editor
   URL + download commands). *This step was merged in from the retired
   upload-notifier Lambda (see below).*

**Environment.** `RAW_PREFIX`, `FINISHED_PREFIX`, `BACKGROUND`, `DDB_TABLE`,
`BEDROCK_MODEL_ID`, `BEDROCK_REGION`, and (for SNS1) `SNS_TOPIC_ARN`,
`STATIC_CARDS_DIR`, `RAW_DOWNLOAD_DIR`, `CORNERS_DOWNLOAD_DIR`, `REVIEW_URL`.

**Permissions.** `s3:GetObject` on `raw/*`; `s3:PutObject` on `raw/*` (corners) and
`finished/*`; `dynamodb:PutItem`; `bedrock:InvokeModel`; `sns:Publish` on the
upload (SNS1) topic.

**⚠ Deploy note.** This is a **container** Lambda, but you don't build it by
hand: `terraform apply` rebuilds + pushes the arm64 image automatically (the
`null_resource` in `ecr.tf` shells out to `docker buildx`) whenever
`Dockerfile`, `app.py`, or `requirements.txt` changes, then points the Lambda at
the new image digest. An **env-var-only** change applies via Terraform with no
rebuild. Requires Docker + buildx on the machine running `apply`; the first build
bakes in the ~224 MB BiRefNet-lite weights and takes several minutes.

---

## 4. review-editor

|                   |                                                                                                                                                                                                                                                                                                 |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Function name** | `BuilderCardsReviewEditor`                                                                                                                                                                                                                                                                      |
| **Source**        | `lambda/review-editor/index.py`                                                                                                                                                                                                                                                                 |
| **Packaging**     | Zip, Python 3.14, `reserved_concurrent_executions = 5`                                                                                                                                                                                                                                          |
| **Trigger**       | API Gateway HTTP API `BuilderCardsAdminApi`                                                                                                                                                                                                                                                     |
| **Routes**        | `GET /review` (list; `?status=rejected` = S3-backed Rejected tab), `GET /review/edit`, `POST /review/save`, `GET /review/delete` (kill-switch confirm) + `POST /review/delete`, `POST /review/reprocess` (put-to-process), `GET /review/reject-delete` (confirm) + `POST /review/reject-delete` |
| **Exposure**      | **Internet, gated by HTTP Basic Auth**                                                                                                                                                                                                                                                          |
| **IAM role**      | `BuilderCardsReviewEditorRole`                                                                                                                                                                                                                                                                  |

**Purpose.** The hosted card editor (deployed version of the local `review.py`).
Lists pending/approved cards, lets you edit fields, and on approval publishes the
card to GitHub (via the committer Lambda).

**Behavior.**
- Server-rendered HTML (no JS). Fields prefill from the `*_ai_draft` values when
  the approved field is still empty, so usually you just tweak + save.
- Shows an inline preview of the finished card via a short-lived pre-signed S3 GET.
- On **save while approved**: invokes the **committer** Lambda synchronously with
  `{action:publish}` to commit (or re-commit) the card to GitHub. **Every approved save republishes**,
  so fixing a typo and saving overwrites the published `<slug>.md`+`.png`. A
  plain save on a still-pending card only updates DynamoDB. (The old SNS2 approval
  email was removed — the GitHub commit is the source of truth.)
- **Rejected tab** (`?status=rejected`): S3-backed, NOT DynamoDB. Reads
  `images/rejected/` **directly from S3** (`list_objects_v2` + presigned preview
  URLs) and shows each picture, labelled "rejected", **with its file size in MB**
  — no card details exist yet. Each has a **Put to process** button (one-click
  `POST /review/reprocess`) and a **Delete** button (confirm page → `POST
  /review/reject-delete` → `DeleteObject` on `rejected/`). No gate, no model
  involved — just S3.
  - **Put to process** reads the rejected bytes and **`put_object`s** them into
    `images/raw/` (NOT `copy_object`) with the `force-process` metadata marker,
    then deletes the rejected copy. It MUST be a PUT: the S3 notification fires
    only on `s3:ObjectCreated:Put`, and `copy_object` emits `:Copy` — a copy
    silently would NOT trigger the gate (this was a real bug, now fixed). The
    gate then sees the marker and forwards to the image-processor without
    re-scoring, so the false negative processes and becomes a pending card. (A
    genuinely-junk reject still ends at the processor's `no_card` — no pending
    card — which is correct.)
- **Image size column**: the list view shows each card's **finished (processed)
  image size in MB** (a `HeadObject` per row on `finishedKey`; `—` if missing),
  and the edit page shows it next to the preview. This is the processed/smaller
  PNG — the analogue of the raw size shown on the Rejected tab. `HeadObject` uses
  the existing `s3:GetObject` on `finished/*`, so no IAM change.

**Security.**
- **HTTP Basic Auth** — username + password read at runtime from the
  `buildercards/review-basic-auth` secret (JSON). Fails **closed** if unset.
- **CSRF** — per-card token = `HMAC(password, cardId)` embedded in the edit form.
  Rejected-tab actions reuse the same scheme keyed on the S3 object key
  (`HMAC(password, key)`), since a rejected upload has no cardId.
- **Input validation** + DynamoDB `attribute_exists(cardId)` condition (never
  creates junk items).
- **Security headers** (CSP, `no-store`, `X-Frame-Options: DENY`, no-referrer).
- **Throttling** at the API stage (5 rps / burst 10) + reserved concurrency 5.
- Holds **no GitHub credentials** — it can only *invoke* the committer.

**Environment.** `DDB_TABLE`, `BASE_PATH`, `BASIC_AUTH_SECRET_ARN`,
`IMAGES_BUCKET`, `COMMITTER_FUNCTION_NAME`,
`RAW_PREFIX` + `REJECTED_PREFIX` (Rejected-tab direct S3). (SNS2 removed. The old
`PUBLISHER_FUNCTION_NAME` + `DELETER_FUNCTION_NAME` are gone — merged into the one
`COMMITTER_FUNCTION_NAME`.)

**Permissions.** DynamoDB `GetItem`/`UpdateItem`/`Query`/`Scan` (table + status
index); `secretsmanager:GetSecretValue` on the basic-auth secret; `s3:GetObject`
on `finished/*` (preview); `s3:ListBucket`/`s3:GetObject`/`s3:DeleteObject` on
the `rejected/*` prefix + `s3:PutObject` on `raw/*` (the Rejected tab: list,
preview, delete, and copy-back for "put to process"); `lambda:InvokeFunction` on
the committer only. (No SNS — the SNS2 publish grant was removed. Still NO
`dynamodb:DeleteItem`, NO `finished/` write, NO GitHub creds — card deletion +
publishing both stay with the committer.)

---

## 5. committer (merged publisher + deleter)

|                    |                                                                           |
| ------------------ | ------------------------------------------------------------------------- |
| **Function name**  | `BuilderCardsCommitter`                                                   |
| **Source**         | `lambda/committer/index.py`                                               |
| **Packaging**      | Zip, Python 3.14                                                          |
| **Trigger**        | Synchronous `lambda.invoke` (RequestResponse) from the review-editor only |
| **Invoke payload** | `{"action": "publish"\|"delete", "cardId": "..."}`                        |
| **Exposure**       | **Internal** — no API Gateway, no public trigger                          |
| **IAM role**       | `BuilderCardsCommitterRole`                                               |

**Purpose.** The single component that holds the GitHub PAT + the destructive
DynamoDB/S3 delete power. It is the **MERGE of the former `publisher` +
`deleter`** Lambdas: they held the *same* github-pat secret, were both INTERNAL
(invoke-only, no API Gateway), and were both called ONLY by the review-editor —
so merging them drops one Lambda + one IAM role **without** moving any
GitHub/delete power into the internet-facing review-editor. Separation of duties
is unchanged: the review-editor still holds NO PAT and NO delete power; it only
has `lambda:InvokeFunction` on this one function, and asks it to publish or
delete via the `action` field. (A compromise of the internet-facing editor still
can't reach the repo or purge data.)

**Behavior — `action: publish`.** Reads the APPROVED card from DynamoDB (source
of truth; verifies `status == "approved"`, never trusts caller content), builds
the Hugo front matter, and commits **directly to the publish branch**
(`var.github_publish_branch`, full-auto = the live branch `master`) in ONE atomic
commit (Git Data API):
- `web/awsbuildercards/content/collectibles/<slug>.md`
- `web/awsbuildercards/static/cards/<cardId>.png` (read from S3 `finishedKey`)

The image is **required** — if it can't be read the whole publish fails, so the
site never gets a `.md` with a missing `.png`. Returns `{published_url}` or
`{error}`. No staging branch, no manual merge, no PR (the review approval is the
human gate). Changing a slug orphans the old `<oldslug>.md` (delete by hand); the
`.png` is keyed by the stable cardId so it always overwrites.

**Behavior — `action: delete`** (the per-card **kill switch**). Reads the card,
then removes it in this ORDER so a mid-way failure is retryable:
1. **GitHub (only if published):** deletes `<slug>.md` + `<cardId>.png` from the
   publish branch (`master`) in ONE atomic commit — this takes a live card OFF
   the site. If the files exist and the delete fails, the whole op ABORTS (S3 +
   DynamoDB untouched) so a retry is clean. A never-published card (empty slug)
   skips this.
2. **S3:** deletes `images/finished/<cardId>.png`, the raw upload (`rawKey`), and
   the raw `<stem>__corners.png` overlay. The bucket is versioned, so these are
   soft deletes (recoverable); missing keys are ignored.
3. **DynamoDB:** deletes the item LAST (guarded by `attribute_exists(cardId)`).

Returns `{"deleted": cardId, "removed": [...], "github": "<commit_url|>"}`
(optionally `"s3_warnings"`) or `{"error": "..."}`.

**Environment** (set in `lambda.tf`). `GITHUB_PAT_SECRET_ARN`, `GITHUB_OWNER`,
`GITHUB_REPO`, `GITHUB_PUBLISH_BRANCH`, `GITHUB_BASE_BRANCH` (the live branch; ==
`GITHUB_PUBLISH_BRANCH` in full-auto, used separately only by the disabled PR
path), `IMAGES_BUCKET`, and `DDB_TABLE` (source of truth for both actions). Note:
`GITHUB_CONTENT_DIR` / `GITHUB_IMAGE_DIR` are NOT set by Terraform — the code
falls back to built-in defaults (`web/awsbuildercards/content/collectibles` and
`web/awsbuildercards/static/cards`).

**Permissions.** `secretsmanager:GetSecretValue` on the `buildercards/github-pat`
secret; `s3:GetObject` on `finished/*` (publish reads the PNG to commit);
`s3:DeleteObject` on `raw/*` + `finished/*` (delete tears down the objects — soft
delete only, NOT `DeleteObjectVersion`); `dynamodb:GetItem` + `dynamodb:DeleteItem`
on the table (publish reads the approved card — PROBLEMS.md #1; delete reads
keys/slug then removes the item). Table only — no GSI. **No SNS.**

**Requirements.** The publish branch must already exist in the repo (the Git Data
API won't create it) and must allow the bot's direct push — no "require a pull
request before merging" protection (that would reject the push). In full-auto the
publish branch is the live branch (`master`), so `master` itself must allow the
direct push; the review approval is the human gate in place of a PR.

**Rollback.** The former `publisher` + `deleter` source is in `X-TRASH/publisher/`
and `X-TRASH/deleter/`. Their functions and roles (`lambda.tf`, `iam.tf`), the
review-editor's old `_invoke_publisher` / `_invoke_deleter` direct-invoke bodies and
the `PUBLISHER_FUNCTION_NAME` / `DELETER_FUNCTION_NAME` env were removed from the
code on 2026-10-26 and are recorded word for word in `HOW_WE_BUILD.md`. To restore
the split: put those back from the record, remove the committer function/role +
the merged `review_editor_invoke_committer` grant, and re-point the review-editor env.

**⚠ Apply note.** Applying this merge **destroys** `BuilderCardsPublisher` +
`BuilderCardsDeleter` (functions + roles) and **creates** `BuilderCardsCommitter`
(function + role). No data is touched (DynamoDB/S3/secrets are unaffected); the
review-editor picks up `COMMITTER_FUNCTION_NAME` in the same apply.

---

## Retired: upload-notifier (merged into image-processor)

`BuilderCardsUploadNotifier` was a standalone Lambda triggered by `finished/`
writes that sent the SNS1 "new card" email. Its logic now lives in the
image-processor (`publish_upload_notification`), which already had all the data
in memory — removing a separate Lambda, a second S3 trigger, and a redundant
DynamoDB read.

The function, its IAM role, its `finished/` S3 trigger, and its output were
removed from `lambda.tf`, `iam.tf`, `s3.tf`, and `outputs.tf` on 2026-10-26 and are
recorded word for word in `HOW_WE_BUILD.md`. The source folder is in
`X-TRASH/upload-notifier/`; restoring the Lambda means moving it back to
`lambda/upload-notifier/` (the recorded `archive_file` points there) and putting
the blocks back from the record.

---

## Secrets (set out-of-band, never in state)

| Secret                           | Consumer      | Contents                                       |
| -------------------------------- | ------------- | ---------------------------------------------- |
| `buildercards/review-basic-auth` | review-editor | JSON `{username, password}` (random)           |
| `buildercards/github-pat`        | committer     | fine-grained PAT (Contents write, single repo) |

See `infrastructure/create-secrets.txt` for the exact `put-secret-value` commands.

## SNS topics

- **SNS1** — upload notifications ("new card, go review"), published by the
  image-processor.
- **SNS2** — `BuilderCardsApprovedCards`, approval confirmation. **RETIRED** (no
  longer wanted): the GitHub commit is the source of truth, so the review editor
  no longer publishes it. Its topic, subscription, IAM, env and code were removed
  from `sns.tf`, `iam.tf`, `lambda.tf`, `outputs.tf`, and
  `lambda/review-editor/index.py` on 2026-10-26 and are recorded word for word in
  `HOW_WE_BUILD.md`.
- **SNS3** — `BuilderCardsDeploymentNotifications`, Amplify deploy
  success/failure email. NOT published by any Lambda: AWS Amplify auto-emits
  "Amplify Deployment Status Change" events to EventBridge, and a rule
  (`eventbridge.tf`) filters them to this app + branch + `SUCCEED`/`FAILED` and
  publishes here (reshaped into a one-line message by an input transformer). The
  Amplify app is NOT managed by Terraform — only referenced by `var.amplify_app_id`.
  Topic/sub in `sns.tf`; rule/target/topic-policy in `eventbridge.tf`.
- **SNS4** — `BuilderCardsRejectedUploads`, card-detect reject email ("an upload
  was auto-rejected as junk, moved to `images/rejected/`"), published by the
  card-detect gate. Its own topic (not SNS1): a reject has no cardId / finished
  image / review page. Topic/sub in `sns.tf`; the gate's `sns:Publish` grant in
  `iam.tf`.
