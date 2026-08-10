<!--
SPDX-License-Identifier: MIT
Copyright (c) 2026 Michal Salanci
-->

# AGENTS.md

Instructions for AI coding agents (and humans) contributing to this repo, in the
open, cross-tool [AGENTS.md](https://agents.md) standard — follow it whichever
agent/IDE you use. Architecture, setup and deploy are in [README.md](README.md);
this file adds the conventions and the change workflow for contributors.

## Orientation

This repo is the serverless backend only: `README.md`, this file, `.gitignore`,
and `infrastructure/` (all Terraform `*.tf`; Lambda source under `lambda/<fn>/`).
The Hugo site the committer publishes into is a **separate repository** — no
site source is here.

Five Lambdas, in pipeline order (the README has the full diagram):

| Lambda            | Ships as                    | Entry      | Trigger                                                                                                               |
| ----------------- | --------------------------- | ---------- | --------------------------------------------------------------------------------------------------------------------- |
| `get-upload-url`  | zip, py3.14                 | `index.py` | public API GW `POST /get-upload-url`; returns a presigned S3 **POST** (5 min, size-capped)                             |
| `card-detect`     | **container**, arm64        | `index.py` | S3 `ObjectCreated:*`, prefix `raw_prefix`, suffix `.jpg` (`s3.tf`) — the stack's only S3 trigger                       |
| `image-processor` | **container**, arm64, 10 GB | `app.py`   | **no S3 trigger** — card-detect async-invokes it (`InvocationType="Event"`), only for uploads that scored card/unsure  |
| `review-editor`   | zip, py3.14                 | `index.py` | admin API GW `/review` routes (HTTP Basic Auth)                                                                        |
| `committer`       | zip, py3.14                 | `index.py` | `lambda.invoke` from review-editor **only**; handles publish *and* delete                                              |

- **DynamoDB `BuilderCards`** (hash `cardId`, GSI `status-index`) is the source
  of truth. The committer re-reads the card and re-verifies `status=approved`
  itself — it never trusts caller input.
- **SNS topics** (`sns.tf`; all subscribed to `notification_email`):
  `upload_notifications` — the "go review" email from image-processor;
  `reject_notifications` — card-detect quarantined an upload to `images/rejected/`;
  `deploy_notifications` — Amplify deploy result (EventBridge rule, `eventbridge.tf`).
- **Region and account are the deployer's**, not constants: `var.aws_region`
  merely defaults to `eu-central-1`. Never hard-code a region, account ID or
  ARN — derive them from variables and data sources as the existing `.tf` do.
- Deep per-Lambda docs: [card-detect](infrastructure/lambda/card-detect/README.md)
  (gate scoring) · [image-processor](infrastructure/lambda/image-processor/README.md)
  (segmentation, memory ceilings, running locally).

## Conventions — IMPORTANT

1. **Comment out replaced code; never delete it. Always explain WHY** in the
   comment. Keep the old version inline (or clearly labeled) for rollback.
   This applies to Terraform blocks too.
2. **Fix stale comments** to the correct values when you touch nearby code —
   don't leave a comment describing the old behavior.
3. **Don't claim a change works until it has run on the deployed stack.**
   `py_compile`/`fmt`/`validate` prove syntax, not behavior. Hand back the
   apply command plus copy-pasteable AWS CLI commands that exercise the change,
   and say plainly: verified locally, untested on the deployed stack.
4. **Secrets are set out-of-band** — never in `terraform.tfvars`, `.tf` files,
   or state. Terraform manages the two secret CONTAINERS only
   (`buildercards/review-basic-auth`, `buildercards/github-pat`; `secrets.tf`);
   values go in via `aws secretsmanager put-secret-value` (README, Setup step 4).
5. **Don't weaken the security model:** the GitHub PAT lives only in the
   internal, invoke-only `committer` — never in the internet-facing
   `review-editor`, which must hold no GitHub write path — and the human
   approval gate stays. The AI only drafts.
6. **Don't commit generated artifacts:** `onnx_out/` model files, `__pycache__`,
   Terraform state, and the zips Terraform writes to `infrastructure/build/`
   are build products, not source.

## How to verify changes (run before handing back)

For any Lambda Python edit, from the repo root:

```
cd infrastructure
python3 -m py_compile lambda/<fn>/index.py   # app.py for image-processor
rm -rf lambda/*/__pycache__
terraform fmt
terraform validate
```

- `terraform validate` should print **Success**. The **only** acceptable warning
  is the pre-existing `hash_key is deprecated` on `dynamodb.tf` line 10 — leave it.
- `terraform fmt` should be a no-op on already-formatted files; run it after any
  `.tf` edit.

## Deploy notes

- **Zip Lambdas** (`get-upload-url`, `review-editor`, `committer`): a code change
  ships on `terraform apply` — Terraform re-zips `lambda/<fn>/index.py`. It zips
  only that one file; a helper module added next to it will not ship.
- **Container Lambdas** (`card-detect`, `image-processor`; ECR, `ecr.tf`): apply
  rebuilds + pushes an image when a hashed file changes — Dockerfile, Python
  entry, `requirements.txt`, plus card-detect's `onnx_out/` model artifacts.
  **Python changes there need that rebuild**; an env-var-only change applies via
  Terraform alone. A new file outside those hash triggers won't force a rebuild.
- `card-detect` cannot build until `onnx_out/` exists — it is git-ignored and
  regenerated once per clone (README, Setup step 2).
- `terraform apply` needs AWS credentials and Docker buildx; it is run by
  whoever operates the deployment (on a fork, that's you). As an agent: verify
  locally with the block above, provide the apply + test commands, and never
  assume a deploy has happened.
