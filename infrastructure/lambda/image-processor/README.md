<!-- SPDX-License-Identifier: MIT
Copyright (c) 2026 Michal Salanci -->

# Image-Processor Lambda (container)

CPU container Lambda that runs the card-cleanup pipeline: rembg/BiRefNet
background removal + OpenCV straighten / crop / rounded-corner fill.

- **Trigger:** invoked asynchronously by the card-detect Lambda, with the S3 event
  of each upload under `images/raw/` that may be a card. It has no S3 trigger of
  its own.
- **Output:** cleaned PNG written to `images/finished/<cardId>.png`, a corners
  overlay next to the upload (`images/raw/<upload name>__corners.png`), a pending
  DynamoDB item with the text a Bedrock vision model drafted, and a "new card"
  email through SNS.
- **Runtime:** Python 3.14 base image, **arm64** (Graviton), CPU only.
- **Model weights:** BiRefNet-lite (~224 MB) baked into the image at build time
  (`U2NET_HOME=/opt/models`), so cold starts do not download anything.

## Files
| File               | Purpose                                                   |
| ------------------ | --------------------------------------------------------- |
| `app.py`           | Lambda handler + pipeline (S3 in -> clean -> S3 out)      |
| `Dockerfile`       | CPU image, Python 3.14 base, weights baked in             |
| `requirements.txt` | rembg, opencv-python-headless, numpy, pillow, pillow-heif |
| `test-event.json`  | sample S3 event for local invoke                          |

## Environment variables (with defaults)
| Var               | Default                 | Meaning                                  |
| ----------------- | ----------------------- | ---------------------------------------- |
| `REMBG_MODEL`     | `birefnet-general-lite` | rembg model                              |
| `BACKGROUND`      | `black`                 | `white` / `black` / `none` (transparent) |
| `RAW_PREFIX`      | `images/raw/`           | input prefix                             |
| `FINISHED_PREFIX` | `images/finished/`      | output prefix                            |

The module docstring of `app.py` lists the other variables.

## Build & test locally

Build the arm64 image (native on an arm64 machine, such as an Apple Silicon Mac):

```bash
cd infrastructure/lambda/image-processor
docker build --platform linux/arm64 -t buildercards-image-processor .
```

The build downloads the BiRefNet-lite weights once (~224 MB) — expect a few minutes.

Run it with the bundled Lambda Runtime Interface Emulator (included in the AWS
base image). It needs AWS creds because the handler reads and writes S3 and
writes a pending item to the DynamoDB table (`BuilderCards` unless `DDB_TABLE`
is set); a failed DynamoDB write fails the record:

```bash
docker run --platform linux/arm64 -p 9000:8080 \
  -v ~/.aws:/root/.aws:ro \
  -e AWS_PROFILE=default \
  -e AWS_REGION=eu-central-1 \
  buildercards-image-processor
```

In another terminal, edit `test-event.json` so the `key` points at a REAL object
under `images/raw/`, then invoke:

```bash
curl -s "http://localhost:9000/2015-03-31/functions/function/invocations" \
  -d @test-event.json | jq
```

Expect `"status": "ok"` and a `finished` key. Check the result:

```bash
aws s3 cp "s3://<images_bucket_name>/images/finished/<cardId>.png" /tmp/out.png
open /tmp/out.png
```

If you want a different background, add `-e BACKGROUND=none` (or `white`) to the
`docker run`.

## Deploy

Terraform deploys it: on `terraform apply` in `infrastructure/`, `ecr.tf`
rebuilds the arm64 image and pushes it to ECR when one of its source files
changed, `lambda.tf` creates the function from the pushed image and gives
card-detect its name, and `iam.tf` lets card-detect invoke it.
