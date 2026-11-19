"""Tiny hand-written .safetensors files: real header layout, zero-filled tensor data, no torch."""

from __future__ import annotations

import json
import struct
from pathlib import Path

# Key names as the diffusers single-file loaders expect them (loaders/single_file_utils.py)
SDXL_KEYS = [
    "conditioner.embedders.0.transformer.text_model.embeddings.position_embedding.weight",
    "conditioner.embedders.1.model.transformer.resblocks.9.mlp.c_proj.bias",
    "model.diffusion_model.label_emb.0.0.weight",
    "model.diffusion_model.input_blocks.0.0.weight",
    "first_stage_model.decoder.conv_in.weight",
]
SD15_KEYS = [
    "cond_stage_model.transformer.text_model.embeddings.position_embedding.weight",
    "model.diffusion_model.input_blocks.0.0.weight",
    "model.diffusion_model.output_blocks.11.0.skip_connection.weight",
    "first_stage_model.decoder.conv_in.weight",
]
# Original Z-Image layout (fused qkv, x_embedder / final_layer), as converted by
# convert_z_image_transformer_checkpoint_to_diffusers
ZIMAGE_TRANSFORMER_KEYS = [
    "x_embedder.weight",
    "cap_embedder.0.weight",
    "noise_refiner.0.attention.qkv.weight",
    "context_refiner.0.attention.qkv.weight",
    "layers.0.adaLN_modulation.0.weight",
    "layers.0.attention.qkv.weight",
    "layers.0.attention.out.weight",
    "final_layer.linear.weight",
]
# ComfyUI all-in-one checkpoint: diffusion model + Qwen3 text encoder + VAE
ZIMAGE_BUNDLE_KEYS = [
    *("model.diffusion_model." + k for k in ZIMAGE_TRANSFORMER_KEYS),
    "text_encoders.qwen3_4b.transformer.model.layers.0.self_attn.q_proj.weight",
    "first_stage_model.decoder.conv_in.weight",
]
# Lumina 2 looks similar but uses adaLN_modulation.1 - must not be taken for Z-Image
LUMINA2_KEYS = ["cap_embedder.0.weight", "layers.0.adaLN_modulation.1.weight"]

# FLUX (not supported): original/ComfyUI checkpoint keys and LoRAs in diffusers / kohya / ComfyUI style
FLUX_KEYS = [
    "model.diffusion_model.double_blocks.0.img_attn.qkv.weight",
    "model.diffusion_model.single_blocks.0.linear1.weight",
]
FLUX_LORA_KEY_SETS = [
    [
        "transformer.single_transformer_blocks.0.attn.to_q.lora_A.weight",
        "transformer.transformer_blocks.1.attn.to_q.lora_A.weight",
    ],
    ["lora_unet_double_blocks_0_img_attn_qkv.lora_down.weight"],
    ["diffusion_model.single_blocks.3.linear1.lora_A.weight"],
]

SDXL_LORA_KEYS = [
    "lora_unet_input_blocks_4_1_transformer_blocks_1_attn1_to_k.lora_down.weight",
    "lora_te2_text_model_encoder_layers_0_mlp_fc1.lora_down.weight",
]
SD15_LORA_KEYS = [
    "lora_unet_down_blocks_0_attentions_0_transformer_blocks_0_attn1_to_k.lora_down.weight",
    "lora_te_text_model_encoder_layers_0_mlp_fc1.lora_down.weight",
]
ZIMAGE_LORA_KEYS = [
    "diffusion_model.layers.0.attention.to_q.lora_A.weight",
    "diffusion_model.layers.0.attention.to_q.lora_B.weight",
]


def write_safetensors(path: Path, keys, metadata: dict | None = None) -> Path:
    header: dict = {}
    if metadata:
        header["__metadata__"] = metadata
    offset = 0
    for key in keys:
        header[key] = {"dtype": "BF16", "shape": [2], "data_offsets": [offset, offset + 4]}
        offset += 4
    raw = json.dumps(header).encode("utf-8")
    raw += b" " * (-len(raw) % 8)
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + b"\0" * offset)
    return path
