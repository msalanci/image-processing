# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci

"""Build-time export of the model files that the card-detect Lambda image bakes in.

It runs with PyTorch on a build machine, never in Lambda, and writes into onnx_out/ next to this
file:

    onnx_out/visual.onnx   the open_clip ViT-B/32 (laion2b) image encoder in ONNX, with its
                           weights in onnx_out/visual.onnx.data
    onnx_out/text.npz      the normalised label text embeddings, logit_scale, aws_count,
                           card_count and the label phrases

so that index.py needs only onnxruntime and numpy, with no PyTorch, text encoder or tokenizer.
The Dockerfile copies these files into the image. Re-run it whenever the labels or the model
change; the labels here are the gate's single source of truth.

The labels come in three groups, kept in this order because index.py slices the softmax by the
group counts:
    AWS_LABELS         the positive class; p_aws is the softmax mass over these. They describe
                       visual cues (the pixel-art icon, the service name, the QR code), which
                       scored better than a brand name alone.
    COMPETITOR_LABELS  other card types, each named so that CLIP puts their probability there
                       instead of on the AWS labels, which keeps p_aws low for them. Adding a
                       label here pushes another card type down.
    NONCARD_LABELS     images that are not cards at all.
p_any_card is the mass over the AWS and competitor groups; index.py uses it only to label a
rejection, never to gate.

MODEL and PRETRAINED name the model of the PyTorch reference run that the ONNX export was
parity-checked against. Changing them changes every score, so the score cutoffs must be measured
again. IMAGE_SIZE must equal IMAGE_SIZE in index.py.

Run, in a virtual environment with build_onnx-requirements.txt installed:
    python build_onnx.py
"""

import os
import numpy as np
import torch
import open_clip

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "onnx_out")

MODEL = "ViT-B-32"
PRETRAINED = "laion2b_s34b_b79k"
IMAGE_SIZE = 224

AWS_LABELS = [
    "an AWS cloud computing trading card",
    "a tech company card with a pixel-art icon, a service name and a QR code",
    "a software product promotional trading card",
]
COMPETITOR_LABELS = [
    "a Pokemon trading card",
    "a Magic: The Gathering trading card",
    "a Yu-Gi-Oh trading card",
    "a standard playing card",
    "a poker playing card",
    "an ice hockey trading card",
    "a basketball trading card",
    "an American football trading card",
    "a baseball trading card",
    "a soccer trading card",
    "a sports trading card with a photo of an athlete",
    "a collectible game card with fantasy artwork",
]
NONCARD_LABELS = [
    "a photo of a person",
    "a selfie",
    "an animal",
    "a landscape photo",
    "a screenshot of an app or website",
    "a piece of food",
    "an everyday object",
    "a sheet of paper or a document",
    "a business card",
    "a cartoon or meme image",
    "a photo of a room or building",
]
ALL_LABELS = AWS_LABELS + COMPETITOR_LABELS + NONCARD_LABELS


def main():
    """Export the image encoder to ONNX and the label embeddings to text.npz, in onnx_out/.

    open_clip downloads the pretrained weights on first use. Only model.visual is exported: it
    returns the same projected image embedding as encode_image, and the text tower is not needed
    at runtime. The batch axis is dynamic, although index.py sends one image at a time. The label
    embeddings are L2-normalised here, so the runtime's dot product with them is a cosine
    similarity.

    Args:
        None. Configuration comes from the module-level MODEL, PRETRAINED, IMAGE_SIZE and label
        constants above.

    Returns:
        None. Writes onnx_out/visual.onnx (with onnx_out/visual.onnx.data) and onnx_out/text.npz
        next to this file, creating onnx_out/ when it is missing, and prints its progress: each
        path it wrote, the label counts and the logit scale.
    """

    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"Loading {MODEL} / {PRETRAINED} (PyTorch, build-time only)...")
    model, _, _ = open_clip.create_model_and_transforms(MODEL, pretrained=PRETRAINED)
    tokenizer = open_clip.get_tokenizer(MODEL)
    model.eval()

    visual_path = os.path.join(OUT_DIR, "visual.onnx")
    dummy = torch.zeros(1, 3, IMAGE_SIZE, IMAGE_SIZE, dtype=torch.float32)
    torch.onnx.export(
        model.visual,
        dummy,
        visual_path,
        input_names=["image"],
        output_names=["embed"],
        dynamic_axes={"image": {0: "batch"}, "embed": {0: "batch"}},
        opset_version=17,
    )
    print(f"  wrote {visual_path}")

    with torch.no_grad():
        tf = model.encode_text(tokenizer(ALL_LABELS))
        tf = tf / tf.norm(dim=-1, keepdim=True)
    text_embeds = tf.numpy().astype(np.float32)
    logit_scale = float(model.logit_scale.exp())

    text_path = os.path.join(OUT_DIR, "text.npz")
    np.savez(
        text_path,
        embeds=text_embeds,
        logit_scale=np.float32(logit_scale),
        aws_count=np.int64(len(AWS_LABELS)),
        card_count=np.int64(len(AWS_LABELS) + len(COMPETITOR_LABELS)),
        labels=np.array(ALL_LABELS),
    )
    print(f"  wrote {text_path}  (labels={len(ALL_LABELS)}, aws={len(AWS_LABELS)}, "
          f"card={len(AWS_LABELS) + len(COMPETITOR_LABELS)}, logit_scale={logit_scale:.2f})")

    print("\nDone. Now run:  python detect_card.py   (torch-free ONNX runtime)")


if __name__ == "__main__":
    main()
