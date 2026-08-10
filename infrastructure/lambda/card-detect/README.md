<!-- SPDX-License-Identifier: MIT
Copyright (c) 2025 Michal Salanci -->

# Card-Detect Gate Lambda (container)

A cheap "is this actually an **AWS** Builder Card?" gate in front of the
expensive image-processor. It owns the **stack's only S3 trigger**
(`images/raw/`, suffix `.jpg` — `s3.tf`), scores each upload with a torch-free
ONNX CLIP classifier, and either **forwards** it into the pipeline (async
invoke of the image-processor) or **quarantines** it to `images/rejected/` and
emails a rejection. No DynamoDB, no Bedrock, no BiRefNet, no GitHub — those
live downstream. Stack-wide setup and deploy:
[repo root README](../../../README.md).

- **Runtime:** `public.ecr.aws/lambda/python:3.14` base image, **arm64**
  (Graviton), CPU only. Sized in `lambda.tf`: 2,048 MB memory, 60 s timeout,
  `reserved_concurrent_executions = 5`. Deliberately not sharing the
  image-processor's container — that one already OOMs at its 10 GB ceiling.
- **Torch-free at runtime:** the Lambda imports only `onnxruntime` + `numpy` +
  `pillow` (+ `pillow-heif` for HEIC, + `boto3` from the base image). No
  PyTorch, no `open_clip`, no tokenizer, no text encoder per request.

**The ONNX split — keep this straight:** PyTorch runs exactly once, on your
machine: `build_onnx.py` (deps: `build_onnx-requirements.txt`) exports the
model into `onnx_out/`, run **before the first `terraform apply`** (root
README, Setup step 2) and again after any label or model change. The deployed
gate runs that export with onnxruntime alone. The export step is unique to
card-detect: the image-processor also runs onnxruntime (via `rembg`) but has
**no export step** — its weights download during its Docker build.

---

## 1. How the scoring works

The model is a **CLIP zero-shot image classifier**: `open_clip` **ViT-B/32**,
pretrained `laion2b_s34b_b79k`. Zero-shot means there is no trained "AWS card"
head — the image is compared against a fixed set of **label phrases**, and
whichever phrases it is closest to win. The "prompt" is that set of phrases.

### The labels (the "prompt")

The phrases live in `build_onnx.py` in **three ordered groups**, encoded to
vectors **once, at build time**, into `onnx_out/text.npz` — so no text encoder
or tokenizer ever runs in the Lambda.

| Group               |  Count | Role                                                                                                         |
| ------------------- | -----: | ------------------------------------------------------------------------------------------------------------ |
| `AWS_LABELS`        |      3 | The **positive class**. Their combined softmax mass = `p_aws`.                                                |
| `COMPETITOR_LABELS` |     12 | Pokemon / MTG / Yu-Gi-Oh / playing & poker cards / sports cards / fantasy cards. Still "a card" — but each **drains** probability away from AWS. |
| `NONCARD_LABELS`    |     11 | person / selfie / animal / screenshot / food / document / etc. — not a card at all.                           |
| **Total**           | **26** |                                                                                                               |

`text.npz` stores: `embeds` (26×512, L2-normalised), `logit_scale` (~100),
`aws_count=3`, `card_count=15` (= AWS + competitor), and the `labels` array.
The label **order** is load-bearing: `[AWS…, competitor…, noncard…]` — the
runtime slices by count and never needs the phrases themselves.

### Runtime path (`p_card()` in `index.py`)

Model and label vectors load once per container at cold start; then per upload:

1. Read raw S3 bytes → Pillow decode (`pillow-heif` handles iPhone HEIC).
2. **Preprocess to match `open_clip` bit-exact** (`preprocess_np`): resize
   shorter side → 224 BICUBIC — the long edge uses `int()` **truncation**, not
   `round()`, which is what made the ONNX output match the PyTorch reference —
   then CenterCrop 224, `/255`, CLIP mean/std normalise.
3. Run the ONNX image encoder (`visual.onnx`, `CPUExecutionProvider`) → a
   512-d embedding.
4. L2-normalise → cosine similarity against the 26 label vectors →
   `× logit_scale` → **softmax over all 26**.
5. Two scores from that one softmax:
   - `p_aws`  = mass over the first `aws_count` (3) AWS labels.
   - `p_card` = mass over the first `card_count` (15) AWS + competitor labels.

### The decision — the gate is on `p_aws`

`index.py` compares `p_aws` to two env-tunable cutoffs, `KEEP_THRESHOLD`
(0.50) and `DROP_THRESHOLD` (0.20):

| `p_aws`                | verdict      | action                                              |
| ---------------------- | ------------ | --------------------------------------------------- |
| `>= KEEP` (0.50)       | `aws-card`   | **forward** to the image-processor (full pipeline)  |
| `DROP <= p_aws < KEEP` | `aws-unsure` | **forward** (abstain *toward* the pipeline)         |
| `< DROP` (0.20)        | *rejected*   | **quarantine** to `images/rejected/` + reject email |

Both forward bands invoke the image-processor **identically** — `KEEP` only
changes the log label. `p_card` is **not** the gate; it only **labels** a
rejection so the log/email can say *why*: `p_card >= DROP` → `non-aws-card`
(a real card, wrong kind); `p_card < DROP` → `not-a-card` (a cat, a house, a
screenshot).

### Why two decisions fall out of one softmax

Softmax is zero-sum, so probability that would otherwise pile onto "an AWS
card" gets **parked** on the most specific matching label instead. A Pokemon
card's mass lands on "a Pokemon trading card"; a cat's on "an animal". Both
leave `p_aws ≈ 0`, so both are rejected — but for logged reasons that differ.
**Adding a card type as its own label is how you subtract points from it** —
that is the entire tuning lever (see "Changing how the gate scores" below).

Measured on the author's test set: real AWS cards scored `p_aws` **0.31–0.87**;
the highest non-AWS upload (a Pokemon card) 0.016, everything else ≤ 0.004.
`DROP=0.20` sits comfortably in that gap.

### Force-process bypass (no re-reject loop)

If the review editor's "put to process" re-submits a false negative, it copies
the object back into `images/raw/` with the S3 metadata marker
`x-amz-meta-force-process` (read as `force-process`; `FORCE_PROCESS_META` in
`index.py`). The gate checks this **before reading the image body** and
forwards without scoring — so a card the model got wrong is not re-rejected
forever.

### Recovery is non-destructive

A rejection **quarantines**, it does not delete: the raw object is copied to
`images/rejected/` (a dead-end prefix nothing triggers on) and the original
deleted — and the bucket is **versioned**, so even that is recoverable. The
reject email includes a ready-to-paste `aws s3 mv …` command; the review
editor's "put to process" is the one-click path back.

---

## 2. What each file does

| File                          | What it does                                                                                                    |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `index.py`                    | The Lambda handler — the scoring and decision above. **Runs in production.**                                     |
| `Dockerfile`                  | Builds the arm64 image: installs `libgomp` (onnxruntime needs OpenMP) + the runtime deps, bakes `onnx_out/` → `/opt/model` and sets `ENV MODEL_DIR=/opt/model`. |
| `requirements.txt`            | **Runtime** pip deps: `onnxruntime`, `numpy`, `pillow`, `pillow-heif` (`boto3` is in the base image).            |
| `.dockerignore`               | Trims the build context: `build_onnx.py`, `build_onnx-requirements.txt`, `.venv`, `__pycache__`, `README.md`. `onnx_out/` is intentionally **not** ignored — the image bakes it in. |
| `build_onnx.py`               | **Build-time** model generator: exports the ViT-B/32 image encoder → `onnx_out/visual.onnx` and precomputes the label vectors → `onnx_out/text.npz`. Not shipped in the image, but **production-critical** — it produces the model the image bakes in. |
| `build_onnx-requirements.txt` | **Build-time** deps for `build_onnx.py` only (`torch`, `open_clip_torch`, `onnx`, …). Heavy; used once in a local venv; **never** in the runtime image. |
| `onnx_out/`                   | **Generated**: `visual.onnx` (graph, ~87 KB), `visual.onnx.data` (~335 MB fp32 weights), `text.npz` (label vectors, ~60 KB). **Git-ignored** (too big for GitHub), regenerable via `build_onnx.py`. |

The Terraform side lives one level up in `infrastructure/`: `ecr.tf`
(`null_resource.card_detect_build` — image build + push), `lambda.tf`
(function, sizing, env vars), `s3.tf` (the trigger), `iam.tf` (read/delete
raw, write rejected, invoke the processor, publish to SNS).

---

## 3. Build & deploy flow

1. **Generate the model once** — venv, `pip install -r
   build_onnx-requirements.txt`, `python build_onnx.py` (exact commands and a
   known `onnxscript` export pitfall: root README, Setup step 2). Re-run
   after any label or model change.
2. **`terraform apply`** — `ecr.tf`'s `null_resource.card_detect_build` fires
   when any trigger hash changes: `Dockerfile`, `index.py`,
   `requirements.txt`, `onnx_out/visual.onnx.data`, `onnx_out/text.npz`. It
   runs `docker buildx build --platform linux/arm64 --provenance=false
   --load`, then `docker push`. `onnx_out/` already sits in the build context,
   so nothing is staged or copied first.
3. **Cold start** — `index.py` loads `/opt/model/visual.onnx` + `text.npz`
   from the image and downloads nothing.

macOS/Docker Desktop apply failures:
[root README → Deploy gotchas](../../../README.md#deploy-gotchas-container-builds).

### Changing how the gate scores — the two levers

**The label phrases live in `build_onnx.py`, not `index.py`.** The runtime
never sees the phrases: it loads `text.npz` and slices the softmax by the
`aws_count` / `card_count` stored next to the vectors. Editing the lists
therefore changes nothing until the export re-runs and the image rebuilds.

- **Labels** (which cards score high): edit `AWS_LABELS` /
  `COMPETITOR_LABELS` / `NONCARD_LABELS` in `build_onnx.py` → `python
  build_onnx.py` (rewrites `text.npz`: new vectors **and** new group counts)
  → `terraform apply` (the changed `text.npz` hash triggers the rebuild).
  Adding a competitor label subtracts probability from `p_aws` for that card
  type; adding an AWS label describes another way a real AWS card can look.
- **Thresholds** (where the cut sits): `KEEP_THRESHOLD` / `DROP_THRESHOLD`
  are plain env vars in `lambda.tf` — `terraform apply` alone, no export, no
  rebuild.

**Check the result** — every scored upload logs one JSON line with `p_aws`,
`p_card`, `verdict` and `action`:

```bash
aws logs tail /aws/lambda/BuilderCardsCardDetect --follow
```

---

## Environment

| Var                             | Value              | Set by           | Meaning                                          |
| ------------------------------- | ------------------ | ---------------- | ------------------------------------------------ |
| `RAW_PREFIX`                    | `images/raw/`      | `lambda.tf`      | Input prefix the gate reads.                     |
| `REJECTED_PREFIX`               | `images/rejected/` | `lambda.tf`      | Quarantine prefix (dead-end).                    |
| `IMAGE_PROCESSOR_FUNCTION_NAME` | (function name)    | `lambda.tf`      | The processor Lambda the gate forwards to.       |
| `REJECT_TOPIC_ARN`              | (topic ARN)        | `lambda.tf`      | The `reject_notifications` SNS topic.            |
| `KEEP_THRESHOLD`                | `0.50`             | `lambda.tf`      | `p_aws >=` → `aws-card` (confident forward).     |
| `DROP_THRESHOLD`                | `0.20`             | `lambda.tf`      | `p_aws <` → reject (quarantine).                 |
| `MODEL_DIR`                     | `/opt/model`       | Dockerfile `ENV` | Where the baked `visual.onnx` + `text.npz` live. |
| `AWS_REGION`                    | (region)           | Lambda runtime   | Provided automatically.                          |

`index.py` carries matching code defaults; an unset target/topic is logged
and skipped, never raised.
