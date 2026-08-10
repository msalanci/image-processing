# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Michal Salanci

"""
BUILD-TIME export (uses PyTorch) — run this ONCE to produce the artifacts the torch-free runtime (detect_card.py) needs:
    onnx_out/visual.onnx   the ViT-B/32 (laion2b) IMAGE encoder, exported to ONNX
    onnx_out/text.npz      precomputed label text-embeddings + logit_scale + the
                           card/non-card split (so the runtime needs no text
                           encoder and no tokenizer)

WHY THIS SPLIT:
PyTorch too heavy for a lambda (~2 GB image, slow cold start). 
So we pay torch ONCE here to export, and the runtime then uses only onnxruntime + numpy,
the exact shape you'd ship in the image-processor container (which already has onnxruntime for BiRefNet).

Run:
    source .venv/bin/activate
    python build_onnx.py
Produces ./onnx_out/. Re-run whenever you change the labels or the model.
"""

import os
import numpy as np
import torch
import open_clip

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "onnx_out")

# MUST match the model the PyTorch test used (so the ONNX run is a true parity check against results.csv)
# and MUST match the labels in that run.
MODEL = "ViT-B-32"
PRETRAINED = "laion2b_s34b_b79k"
IMAGE_SIZE = 224  # ViT-B/32 input resolution

# Label phrases, in 3 groups (order matters — see the counts saved below):
#   AWS_LABELS
#   COMPETITOR_LABELS
#   NONCARD_LABELS
# p_card (any card) = mass over AWS + competitor. 
# 
# The runtime (index.py) GATES on p_aws: p_aws >= DROP -> pipeline; 
# p_aws < DROP -> rejected. 
# p_card only LABELS a rejection "non-aws-card" (real card, wrong kind) vs "not-a-card" (cat/house).
AWS_LABELS = [
    "an AWS cloud computing trading card",
    "a tech company card with a pixel-art icon, a service name and a QR code",
    "a software product promotional trading card",
]
# Competitor card types to subtract points from (each named so CLIP parks its probability here, not on the AWS labels). 
# Add more types as needed.
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
# Anything not being a card of any kind
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
# Order: AWS first, then competitors, then non-card (the runtime slices by count).
ALL_LABELS = AWS_LABELS + COMPETITOR_LABELS + NONCARD_LABELS


def main():
    """
    Exports the two artifacts the torch-free runtime needs, and writes them to onnx_out/.

    The CLIP model is loaded once with PyTorch and put in evaluation mode. 
    Its image encoder is traced with a dummy input and exported to ONNX, so the runtime can produce image embeddings with onnxruntime alone. 

    Args:
        None. Configuration comes from the module-level MODEL, PRETRAINED, IMAGE_SIZE and label constants above.

    Returns:
        None. Writes onnx_out/visual.onnx and onnx_out/text.npz as a side effect, and prints the path and label counts for each.
    """

    # Load the CLIP model and tokenizer
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"Loading {MODEL} / {PRETRAINED} (PyTorch, build-time only)...")
    model, _, _ = open_clip.create_model_and_transforms(MODEL, pretrained=PRETRAINED)
    tokenizer = open_clip.get_tokenizer(MODEL)
    model.eval()

    # Export the image encoder to ONNX
    visual_path = os.path.join(OUT_DIR, "visual.onnx")
    dummy = torch.zeros(1, 3, IMAGE_SIZE, IMAGE_SIZE, dtype=torch.float32)
    torch.onnx.export(
        model.visual,
        dummy,
        visual_path,
        input_names=["image"],
        output_names=["embed"],
        # allow variable batch size (runtime sends 1 at a time)
        dynamic_axes={"image": {0: "batch"}, "embed": {0: "batch"}},
        opset_version=17,
    )
    print(f"  wrote {visual_path}")

    # Precompute the label text-embeddings
    with torch.no_grad():
        tf = model.encode_text(tokenizer(ALL_LABELS))
        tf = tf / tf.norm(dim=-1, keepdim=True)
    text_embeds = tf.numpy().astype(np.float32)
    logit_scale = float(model.logit_scale.exp())

    # Save the vectors plus the counts
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
