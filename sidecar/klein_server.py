"""Local stills sidecar: FLUX.2 [klein] 4B on your GPU, behind a tiny HTTP API.

Run (from this folder):  uv run python klein_server.py [--port 8790] [--offload]
The music video app sends stills here when a song's Stills model is
"local/flux2-klein-4b". One image at a time; requests queue.

  GET  /health    {"ok", "model", "mode", "busy", "waiting"}
  POST /generate  {"prompt", "refs": [{"label", "image": <base64>}], "width", "height",
                   "steps", "seed"}  ->  {"image": <base64 PNG>, "seed", "seconds"}

Memory: the image model (about 8 GB) stays on the GPU and the text encoder
runs on the CPU, which is about 5 s per image. If the GPU is too full for that
(another program holding VRAM), it falls back to moving each part onto the GPU
only while it runs: about 25 s per image. --offload starts in that mode.
"""
import base64
import io
import json
import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch
from diffusers import Flux2KleinPipeline
from PIL import Image

MODEL = "black-forest-labs/FLUX.2-klein-4B"
REF_SIDE = 1024   # references are downscaled to this before conditioning

state = {"mode": None, "busy": False, "waiting": 0}
gpu = threading.Lock()


def load(offload):
    pipe = Flux2KleinPipeline.from_pretrained(MODEL, torch_dtype=torch.bfloat16)
    if offload:
        pipe.enable_model_cpu_offload()
        state["mode"] = "offload"
    else:
        pipe.transformer.to("cuda")
        pipe.vae.to("cuda")
        state["mode"] = "fast"
    return pipe


def generate(pipe, prompt, refs, width, height, steps, seed):
    images = None
    if refs:
        names = ", ".join(f"image {n + 1} is {r['label']}" for n, r in enumerate(refs))
        prompt = (f"Reference images: {names}. Keep these characters, places and props "
                  f"looking exactly like their references. {prompt}")
        images = []
        for r in refs:
            im = Image.open(io.BytesIO(base64.b64decode(r["image"]))).convert("RGB")
            im.thumbnail((REF_SIDE, REF_SIDE))
            images.append(im)
    gen = torch.Generator("cuda").manual_seed(seed)
    if state["mode"] == "fast":
        with torch.no_grad():
            emb = pipe.encode_prompt(prompt, device=torch.device("cpu"))
        emb = (emb[0] if isinstance(emb, tuple) else emb).to("cuda")
        return pipe(prompt_embeds=emb, image=images, width=width, height=height, guidance_scale=1.0,
                    num_inference_steps=steps, generator=gen).images[0]
    return pipe(prompt=prompt, image=images, width=width, height=height, guidance_scale=1.0,
                num_inference_steps=steps, generator=gen).images[0]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self.send(200, {"ok": True, "model": MODEL, **state})
        self.send(404, {"error": "Not found"})

    def do_POST(self):
        global pipe
        if self.path != "/generate":
            return self.send(404, {"error": "Not found"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            prompt = req["prompt"]
        except (ValueError, KeyError):
            return self.send(400, {"error": "Send JSON with a prompt."})
        width = int(req.get("width", 1360)) // 16 * 16
        height = int(req.get("height", 768)) // 16 * 16
        steps = int(req.get("steps", 4))
        seed = int(req["seed"]) if req.get("seed") is not None else random.randrange(2**31)
        state["waiting"] += 1
        with gpu:
            state["waiting"] -= 1
            state["busy"] = True
            t = time.time()
            try:
                try:
                    img = generate(pipe, prompt, req.get("refs") or [], width, height, steps, seed)
                except torch.OutOfMemoryError:
                    if state["mode"] != "fast":
                        raise
                    # Something else took VRAM: drop to offload mode for good and retry.
                    print("GPU too full for fast mode; switching to offload mode.", flush=True)
                    del pipe
                    torch.cuda.empty_cache()
                    pipe = load(offload=True)
                    img = generate(pipe, prompt, req.get("refs") or [], width, height, steps, seed)
            except Exception as e:
                torch.cuda.empty_cache()
                print(f"failed: {type(e).__name__}: {e}", flush=True)
                return self.send(500, {"error": f"{type(e).__name__}: {e}"[:500]})
            finally:
                state["busy"] = False
            buf = io.BytesIO()
            img.save(buf, "PNG")
            secs = round(time.time() - t, 1)
            print(f"{width}x{height} {len(req.get('refs') or [])} refs, {secs}s ({state['mode']})", flush=True)
        self.send(200, {"image": base64.b64encode(buf.getvalue()).decode(), "seed": seed, "seconds": secs})


if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8790
    torch.set_num_threads(max(4, (os.cpu_count() or 8) // 2))
    from huggingface_hub import try_to_load_from_cache
    if not isinstance(try_to_load_from_cache(MODEL, "model_index.json"), str):
        print(f"First run: downloading {MODEL} (about 15 GB) into the Hugging Face cache. "
              f"This happens once; later starts take a few seconds.", flush=True)
    print(f"Loading {MODEL}...", flush=True)
    pipe = load(offload="--offload" in sys.argv)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    print(f"Klein sidecar on http://127.0.0.1:{port} ({state['mode']} mode)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
