<!-- SPDX-License-Identifier: MIT
Copyright (c) 2026 Michal Salanci -->

# Image-Processor Lambda (container)

CPU container Lambda that runs the proven card-cleanup pipeline
(`local-tests/bg-removal/straighten_quad.py`): rembg/BiRefNet background removal
+ OpenCV straighten / crop / rounded-corner fill.

- **Trigger (intended):** S3 `ObjectCreated:Put` under `images/raw/`.
- **Output:** cleaned PNG written to `images/finished/<same-name>.png`.
- **Runtime:** Python 3.14 base image, **arm64** (Graviton), CPU only.
- **Model weights:** BiRefNet-lite (~224 MB) baked into the image at build time
  (`U2NET_HOME=/opt/models`), so cold starts do not download anything.

## Files
| File               | Purpose                                              |
| ------------------ | ---------------------------------------------------- |
| `app.py`           | Lambda handler + pipeline (S3 in -> clean -> S3 out) |
| `Dockerfile`       | CPU image, Python 3.12 base, weights baked in        |
| `requirements.txt` | rembg, opencv-python-headless, numpy, pillow         |
| `test-event.json`  | sample S3 event for local invoke                     |

## Environment variables (with defaults)
| Var               | Default            | Meaning                                  |
| ----------------- | ------------------ | ---------------------------------------- |
| `REMBG_MODEL`     | `birefnet-general` | rembg model                              |
| `BACKGROUND`      | `white`            | `white` / `black` / `none` (transparent) |
| `RAW_PREFIX`      | `images/raw/`      | input prefix                             |
| `FINISHED_PREFIX` | `images/finished/` | output prefix                            |

## Phase A — build & test LOCALLY (do this first)

Build the arm64 image on the M4 mac (native, no emulation):

```bash
cd infrastructure/lambda/image-processor
docker build --platform linux/arm64 -t buildercards-image-processor .
```

The build downloads the BiRefNet-lite weights once (~224 MB) — expect a few minutes.

Run it with the bundled Lambda Runtime Interface Emulator (included in the AWS
base image). It needs AWS creds because the handler reads/writes S3:

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
aws s3 cp "s3://<images_bucket_name>/images/finished/<name>.png" /tmp/out.png
open /tmp/out.png
```

If you want a different background, add `-e BACKGROUND=none` (or `black`) to the
`docker run`.

## Phase B — deploy (next step, not yet wired)
1. Create an ECR repo, build for arm64, push the image.
2. `aws_lambda_function` with `package_type = "IMAGE"`, `image_uri`, an IAM role
   that can `s3:GetObject` raw + `s3:PutObject` finished, memory ~4096 MB,
   timeout ~120 s, architecture `arm64`.
3. Wire the S3 trigger. NOTE: S3 does not allow two Lambda notifications on the
   same prefix+event, and the UploadNotifier already uses
   `images/raw/ + ObjectCreated:Put`. Pick one:
   - **EventBridge fan-out** (S3 -> EventBridge -> both Lambdas) — cleanest for
     multiple consumers, and
   - or trigger the processor on `images/raw/` and move the notifier to
     `images/finished/` (notify when the cleaned card is ready).
   Decide before changing the live trigger.
