"""One-off benchmark: can LTX-2.5 (distilled, Q4 GGUF) animate a still on this machine?

Usage (from sidecar/):
  uv run --with bitsandbytes --with gguf --with av --with imageio --with imageio-ffmpeg \
      python ltx_bench.py <still.png> "<motion prompt>" [seconds] [out.mp4]

First run downloads about 45 GB (unsloth/LTX-2.5-FP8 parts + the distilled Q4 GGUF).
Findings on an RTX 4080 Super 16 GB / 30 GB RAM (2026-10-31): a 4 s clip at
832x480 renders in ~3 min and looks clean; 8 s drifts by the end; 12 s melts
after ~4 s; 15 s runs out of VRAM. Not wired into the app: too heavy for most
machines, and Grok Imagine Video 1.5 Lite is $0.02/s.

Works in phases so only one big part is on the GPU at a time:
  1. Gemma 4 text encoder in 4-bit -> prompt embeddings, then freed
  2. transformer (GGUF Q4) streamed onto the GPU in blocks -> latents
  3. VAE decode -> frames (the pipeline's audio is discarded)
Prints time, peak VRAM and peak RAM for each phase.
"""
import gc
import resource
import sys
import time

import torch
from diffusers import GGUFQuantizationConfig, LTX2ImageToVideoPipeline, LTX2VideoTransformer3DModel
from diffusers.utils import export_to_video
from huggingface_hub import hf_hub_download
from PIL import Image
from transformers import BitsAndBytesConfig

PARTS = "unsloth/LTX-2.5-FP8"
GGUF = ("unsloth/LTX-2.5-GGUF", "distilled/ltx-2.5-22b-distilled-Q4_K_M.gguf")
W, H, FPS = 832, 480, 24


def patch_ltx25_keys():
    """diffusers' LTX-2 single-file converter names LTX-2.5's prompt AdaLN
    layers "*_adaln_single"; the 2.5 model calls them "*_adaln"."""
    from diffusers.loaders import single_file_model, single_file_utils
    orig = single_file_utils.convert_ltx2_transformer_to_diffusers

    def convert(checkpoint, **kwargs):
        sd = orig(checkpoint, **kwargs)
        return {k.replace("prompt_adaln_single.", "prompt_adaln."): v for k, v in sd.items()}

    single_file_utils.convert_ltx2_transformer_to_diffusers = convert
    for spec in single_file_model.SINGLE_FILE_LOADABLE_CLASSES.values():
        if spec.get("checkpoint_mapping_fn") is orig:
            spec["checkpoint_mapping_fn"] = convert   # export is 854x480; LTX wants multiples of 32


def mark(label, t):
    peak_ram = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
    print(f"{label}: {time.time() - t:.1f}s, peak VRAM {torch.cuda.max_memory_allocated() / 1e9:.1f} GB, "
          f"peak RAM {peak_ram:.1f} GB", flush=True)


def main():
    still, prompt = sys.argv[1], sys.argv[2]
    seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 4.0
    out = sys.argv[4] if len(sys.argv) > 4 else "ltx_bench.mp4"
    frames = int(seconds * FPS) // 8 * 8 + 1   # LTX wants 8k+1 frames
    image = Image.open(still).convert("RGB").resize((W, H))

    # Phase 1: text encoder in 4-bit on the GPU, then drop it, before the
    # transformer is loaded, so the two are never in RAM together.
    t = time.time()
    pipe = LTX2ImageToVideoPipeline.from_pretrained(PARTS, transformer=None, text_encoder=None, duration_head=None, prompt_enhancer=None, processor=None, torch_dtype=torch.bfloat16)
    from transformers import Gemma4UnifiedForConditionalGeneration
    te = Gemma4UnifiedForConditionalGeneration.from_pretrained(
        PARTS, subfolder="text_encoder", torch_dtype=torch.bfloat16, device_map="cuda",
        quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16))
    pipe.text_encoder = te
    with torch.no_grad():
        enc = pipe.encode_prompt(prompt=prompt, negative_prompt="", do_classifier_free_guidance=True, device="cuda")
    enc = [e.cpu() if e is not None else None for e in enc]
    pipe.text_encoder = None
    del te
    gc.collect(); torch.cuda.empty_cache()
    mark("text encoder (4-bit): load + encode", t)

    t = time.time()
    patch_ltx25_keys()
    pipe.transformer = LTX2VideoTransformer3DModel.from_single_file(
        hf_hub_download(*GGUF), config=PARTS, subfolder="transformer",
        quantization_config=GGUFQuantizationConfig(compute_dtype=torch.bfloat16), torch_dtype=torch.bfloat16)
    mark("load transformer (GGUF Q4, CPU)", t)

    # Phase 2+3: transformer (13.6 GB Q4) on the GPU for the denoising steps, then the VAE.
    t = time.time(); torch.cuda.reset_peak_memory_stats()
    # Stream the weights through the GPU a few blocks at a time. The whole Q4
    # transformer (13.6 GB) plus activations doesn't fit in 16 GB, and
    # use_stream=True pins a second copy in RAM (OOM-killed at 30 GB).
    from diffusers.hooks import apply_group_offloading as apply_group_offload
    # Only the transformer streams (about 2 GB of it on the GPU at a time); the
    # rest lives on the GPU, which keeps RAM down: connectors 6.3 GB, VAEs ~2 GB.
    apply_group_offload(pipe.transformer, onload_device=torch.device("cuda"), offload_device=torch.device("cpu"),
                        offload_type="block_level", num_blocks_per_group=1, use_stream=False)
    for part in (pipe.connectors, pipe.vae, pipe.audio_vae, pipe.vocoder):
        part.to("cuda")
    pipe.vae.enable_tiling()

    # The connectors run once, at the start of a clip. Drop their 6.3 GB from
    # the GPU as soon as they have, so longer clips have room to denoise.
    calls = []
    def free_connectors(module, args, output):
        calls.append(1)
        module.to("meta")
        torch.cuda.empty_cache()
        return output
    pipe.connectors.register_forward_hook(free_connectors)
    gc.collect()
    enc = [e.to("cuda") if e is not None else None for e in enc]
    result = pipe(image=image, prompt_embeds=enc[0], prompt_attention_mask=enc[1],
                  negative_prompt_embeds=enc[2], negative_prompt_attention_mask=enc[3],
                  width=W, height=H, num_frames=frames, frame_rate=FPS,
                  num_inference_steps=8, guidance_scale=1.0,
                  generator=torch.Generator("cuda").manual_seed(0))
    print(f"connectors ran {len(calls)}x", flush=True)
    mark(f"generate {frames} frames ({frames / FPS:.1f}s of video)", t)
    video = result.frames[0]
    export_to_video(video, out, fps=FPS)
    print("wrote", out, flush=True)


if __name__ == "__main__":
    main()
