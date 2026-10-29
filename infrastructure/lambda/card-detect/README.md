<!-- SPDX-License-Identifier: MIT
Copyright (c) 2026 Michal Salanci -->

# Card-Detect Gate Lambda (container)

A cheap "is this actually an **AWS** Builder Card?" gate that sits IN FRONT of the
expensive image-processor. It owns the S3 `images/raw/*.jpg` trigger, scores each
upload with a torch-free ONNX CLIP classifier, and either **forwards** the card
into the pipeline (async invoke of the image-processor) or **quarantines** the
raw object to `images/rejected/` and emails a rejection (SNS4). No DynamoDB, no
Bedrock, no BiRefNet, no GitHub — those all live downstream.

- **Runtime:** Python 3.14 base image, **arm64** (Graviton), CPU only. Isolated
  from the image-processor's 10 GB (the processor already OOMs at its ceiling, so
  the model must NOT share its container).
- **Torch-free at runtime:** the Lambda imports only `onnxruntime` + `numpy` +
  `pillow` (+ `pillow-heif` for HEIC, + `boto3` from the base image). No PyTorch,
  no `open_clip`, no tokenizer, no text encoder run per request.

This README covers exactly three things: **(1)** how the score is computed and how
the gate decides, **(2)** what each file does, and **(3)** which files ship or
build the gate.

---

## 1. How the scoring works

The model is a **CLIP zero-shot image classifier**: `open_clip` **ViT-B/32**,
pretrained `laion2b_s34b_b79k`. Zero-shot means there is no trained "AWS card"
head — instead the image is compared against a fixed set of **label phrases**,
and whichever phrases it is closest to win. The "prompt" here is not a string;
it is that set of label phrases.

### The labels (the "prompt")

The phrases live in `build_onnx.py` in **three ordered groups**. They are encoded
to vectors **once, at build time**, into `onnx_out/text.npz` — so no text encoder
or tokenizer ever runs in the Lambda.

| Group              | Count | Role                                                                                   |
| ------------------ | ----: | -------------------------------------------------------------------------------------- |
| `AWS_LABELS`       |     3 | The **positive class**. Their combined softmax mass = `p_aws`.                          |
| `COMPETITOR_LABELS`|    12 | Pokemon / MTG / Yu-Gi-Oh / poker / NHL / NBA / NFL / baseball / soccer / sports / fantasy. Still "a card" — but each **drains** probability away from AWS. |
| `NONCARD_LABELS`   |    11 | person / selfie / animal / screenshot / food / document / etc. — not a card at all.    |
| **Total**          |    **26** |                                                                                    |

`text.npz` stores: `embeds` (26×512, L2-normalised), `logit_scale` (~100),
`aws_count=3`, `card_count=15` (= AWS + competitor), and the `labels` array. The
label **order** is load-bearing: `[AWS…, competitor…, noncard…]`. The runtime
slices by count, so it never needs to know the phrases themselves.

### Runtime path (`p_card()` in `index.py`)

Per upload, loaded once per container at cold start, then reused warm:

1. Read raw S3 bytes → `Pillow` decode (`pillow-heif` handles iPhone HEIC).
2. **Preprocess to match `open_clip` bit-exact:** Resize shorter side → 224
   BICUBIC (long edge uses `int()` **truncation**, not `round()` — this is what
   made the ONNX output match the PyTorch reference), CenterCrop 224, `/255`,
   then CLIP mean/std normalise.
3. Run the **ONNX image encoder** (`visual.onnx`, `CPUExecutionProvider`) → a
   512-d embedding.
4. L2-normalise → cosine similarity against the 26 label vectors → `× logit_scale`
   → **softmax over all 26**.
5. Produce **two** scores from that single softmax:
   - `p_aws`  = sum over the first `aws_count` (3) AWS labels.
   - `p_card` = sum over the first `card_count` (15) AWS + competitor labels.

### The decision — the gate is on `p_aws`

`index.py` compares `p_aws` to two env-tunable cutoffs, `KEEP_THRESHOLD` (0.50)
and `DROP_THRESHOLD` (0.20):

| `p_aws`                         | verdict        | action                                                        |
| ------------------------------- | -------------- | ------------------------------------------------------------- |
| `>= KEEP` (0.50)                | `aws-card`     | **forward** to the image-processor (full pipeline)            |
| `DROP <= p_aws < KEEP`          | `aws-unsure`   | **forward** (abstain *toward* the pipeline)                   |
| `< DROP` (0.20)                 | *rejected*     | **quarantine** to `images/rejected/` + SNS4 reject email      |

Both forward bands invoke the image-processor **identically** — `KEEP` only
changes the log label (confident vs unsure). `p_card` is **not** the gate; it only
**labels a rejection** so the log/email can say *why*:

- `p_card >= DROP` → `non-aws-card` (a real card, wrong kind — Pokemon/poker/sports).
- `p_card <  DROP` → `not-a-card` (a cat, a house, a screenshot).

### Why two decisions fall out of one softmax

Softmax is zero-sum, so probability that would otherwise pile onto "an AWS card"
gets **parked** on the most specific matching label instead. A Pokemon card's
mass lands on "a Pokemon trading card"; a cat's lands on "an animal". Both leave
`p_aws ≈ 0`, so both are rejected — but for logged reasons that differ. Only a
genuine AWS card keeps a high `p_aws`. **Adding a card type as its own label is
how you subtract points from it** — that is the entire tuning lever (see
"Changing which cards score high" below).

### Measured example (`KEEP=0.50`, `DROP=0.20`)

| Upload             | `p_aws`   | Outcome              |
| ------------------ | --------- | -------------------- |
| Real AWS cards     | 0.31–0.87 | forwarded (pipeline) |
| Pokemon            | 0.016     | rejected             |
| Baseball           | 0.004     | rejected             |
| NHL                | 0.0002    | rejected             |
| Poker              | 0.000     | rejected             |
| Cat                | 0.000     | rejected             |

`DROP=0.20` sits comfortably in the gap between the lowest real AWS card (~0.31)
and the highest non-AWS upload (~0.016).

### Force-process bypass (no re-reject loop)

If the review editor's "put to process" re-submits a false negative, it copies
the object back into `images/raw/` with the S3 metadata marker
`x-amz-meta-force-process` (read as `force-process`; Terraform passes this name
to the gate and the review editor as `FORCE_PROCESS_META`). The gate checks this
**before it even reads the image body** and forwards without scoring — so a
genuine card the model got wrong is not scored identically and re-rejected
forever.

### Recovery is non-destructive

A rejection **quarantines**, it does not delete: the raw object is copied to
`images/rejected/` (a dead-end prefix nothing triggers on) and the original is
deleted — but the bucket is **versioned**, so even that is recoverable. The SNS4
email links the review editor's Rejected tab, whose "Put to process" button is
the one-click path back.

---

## 2. What each file does

See section 3 for the short list; this is the per-file reference.

**In this directory (`infrastructure/lambda/card-detect/`):**

| File                          | What it does                                                                                                     |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `index.py`                    | The Lambda handler. Does all the scoring and the decision above. **Runs in production.**                        |
| `Dockerfile`                  | Builds the arm64 image: installs runtime deps and bakes `onnx_out/` → `/opt/model` (`MODEL_DIR`).               |
| `requirements.txt`            | **Runtime** pip deps: `onnxruntime`, `numpy`, `pillow`, `pillow-heif`. (`boto3` is already in the base image.)  |
| `.dockerignore`              | Allowlist for the build context: only `requirements.txt`, `index.py` and the three `onnx_out/` files reach Docker, so `build_onnx.py`, `build_onnx-requirements.txt`, `.venv`, `__pycache__` and `README.md` stay out. |
| `build_onnx.py`               | **Build-time** model generator. Exports the ViT-B/32 image encoder → `onnx_out/visual.onnx` and precomputes the label vectors → `onnx_out/text.npz`. Not shipped in the image, but **production-critical** — it produces the model the image bakes in. Run once before the first apply, and again whenever you change the labels or the model. |
| `build_onnx-requirements.txt` | **Build-time** deps for `build_onnx.py` only: `torch`, `open_clip_torch`, `numpy`, `onnx`. Heavy; used once in a local venv; **never** in the runtime image. |
| `onnx_out/`                   | **Generated** artifacts: `visual.onnx` (graph, ~87 KB), `visual.onnx.data` (~335 MB fp32 weights), `text.npz` (label vectors, ~60 KB). **Git-ignored** (too big for GitHub) and regenerable via `build_onnx.py`. `ecr.tf` hashes all three to decide when to rebuild; the Dockerfile COPYs the three files → `/opt/model`. |

**In `infrastructure/` (referenced, not in this dir):**

| File                          | What it does                                                                                                     |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `ecr.tf` → `null_resource.card_detect_build` | Builds and pushes the arm64 image on `terraform apply` when any trigger hash changes. There is **no staging step** — `onnx_out/` already sits in the build context. |
| `lambda.tf`                   | Wires the gate's env vars, memory/timeout, S3 trigger, and IAM (the env vars in section "Environment" below).   |

---

## 3. Production and build-critical files

> **Rule of thumb:** everything in **this** Lambda directory ships or builds the
> gate, and nothing outside it is needed to build or run it.

The deployed Lambda plus everything that builds its image:

- `index.py` — the handler that runs in production (scoring + decision).
- `Dockerfile` — builds the arm64 image; bakes `onnx_out/` → `/opt/model`.
- `requirements.txt` — runtime deps.
- `.dockerignore` — trims the build context.
- `build_onnx.py` — build-time model generator (not shipped, but produces the
  model the image bakes in — **production-critical**).
- `build_onnx-requirements.txt` — build-time deps for `build_onnx.py` only.
- `onnx_out/` — generated model artifacts baked into the image (git-ignored,
  regenerable).

---

## Build / deploy flow (no staging step)

1. **Generate the model once** (and again whenever labels or the model change):

   ```bash
   cd infrastructure/lambda/card-detect
   python3 -m venv .venv && source .venv/bin/activate
   pip install -U pip && pip install -r build_onnx-requirements.txt
   python build_onnx.py     # writes onnx_out/{visual.onnx, visual.onnx.data, text.npz} in place
   ```

2. **`terraform apply`** → `ecr.tf`'s `null_resource.card_detect_build` fires when
   any of its trigger hashes change: `Dockerfile`, `.dockerignore`, `index.py`,
   `requirements.txt`, `onnx_out/visual.onnx`, `onnx_out/visual.onnx.data`,
   `onnx_out/text.npz`. On a change it runs
   `docker buildx build --platform linux/arm64 --provenance=false --load` and
   `docker push`. Because `onnx_out/` already lives in the build context, **nothing
   is staged or copied**.

3. The **Dockerfile** bakes `onnx_out/` → `/opt/model` (`MODEL_DIR`) and installs
   the runtime deps.

4. **Cold start:** `index.py` loads `/opt/model/visual.onnx` + `text.npz` from the
   local filesystem and downloads nothing.

### Changing which cards score high

Edit the label lists in `build_onnx.py` → re-run `build_onnx.py` → `terraform
apply`. The regenerated `text.npz` changes its hash, which flips the
`null_resource.card_detect_build` trigger and rebuilds the image. Adding a
competitor label subtracts probability from `p_aws` for that card type; adding an
AWS label describes another way a real AWS card can look.

---

## Environment (set by Terraform)

| Var                             | Default             | Meaning                                              |
| ------------------------------- | ------------------- | --------------------------------------------------- |
| `RAW_PREFIX`                    | `images/raw/`       | Input prefix the gate reads.                         |
| `REJECTED_PREFIX`               | `images/rejected/`  | Quarantine prefix (dead-end).                        |
| `IMAGE_PROCESSOR_FUNCTION_NAME` | (set by TF)         | The processor Lambda the gate forwards to.           |
| `REJECT_TOPIC_ARN`              | (set by TF)         | SNS4 reject-email topic.                             |
| `REJECTED_TAB_URL`              | (set by TF)         | Review editor's Rejected tab, linked from the email. |
| `FORCE_PROCESS_META`            | `force-process`     | Metadata key that forwards an upload unscored; the review editor sets the same key. |
| `KEEP_THRESHOLD`                | `0.50`              | `p_aws >=` → `aws-card` (confident forward).         |
| `DROP_THRESHOLD`                | `0.20`              | `p_aws <` → reject (quarantine).                     |
| `MODEL_DIR`                     | `/opt/model`        | Where the baked `visual.onnx` + `text.npz` live.     |
| `AWS_REGION`                    | (Lambda runtime)    | Provided automatically.                              |
