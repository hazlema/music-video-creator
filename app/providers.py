"""Every paid call goes through here: OpenRouter for transcription, the
storyboard LLM, stills and video.

Stills sit behind StillProvider so a local Flux model can replace
OpenRouterStills later without touching the rest of the app.
"""
import base64
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mv"))
from common import api_key  # noqa: E402

BASE = "https://openrouter.ai/api/v1"


class ProviderError(Exception):
    pass


def _request(path_or_url, body=None, timeout=600, raw=False):
    url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
    headers = {"Authorization": f"Bearer {api_key()}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except Exception:
            pass
        raise ProviderError(f"HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise ProviderError(f"Network error: {e.reason}") from None
    return payload if raw else json.loads(payload)


def data_url(path, max_side=1536):
    """Image file -> JPEG data URL, downscaled so requests stay small."""
    from PIL import Image

    im = Image.open(path).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------- transcribe

def transcribe(wav_bytes):
    data = _request("/audio/transcriptions", {
        "model": "nvidia/nemotron-3.5-transcribe",
        "input_audio": {"data": base64.b64encode(wav_bytes).decode(), "format": "wav"},
        "response_format": "verbose_json",
    })
    cost = (data.get("usage") or {}).get("cost") or 0
    return data, cost


# ---------------------------------------------------------------------- chat

def chat_json(model, system, user, max_tokens=16000):
    data = _request("/chat/completions", {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
    })
    text = data["choices"][0]["message"]["content"] or ""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ProviderError("The storyboard model didn't return JSON.")
        parsed = json.loads(m.group(0))
    return parsed, (data.get("usage") or {}).get("cost") or 0


# -------------------------------------------------------------------- stills

class StillProvider:
    """generate(prompt, refs) -> (image bytes, file extension, cost in USD).
    refs is a list of (label, path) pairs."""

    def generate(self, prompt, refs):
        raise NotImplementedError


class OpenRouterStills(StillProvider):
    def __init__(self, model):
        self.model = model

    def generate(self, prompt, refs):
        content = []
        if refs:
            names = ", ".join(f"image {n + 1}: {label}" for n, (label, _) in enumerate(refs))
            content.append({"type": "text", "text":
                            f"Reference images ({names}). Keep these characters, places and props "
                            f"looking exactly like their references.\n\n"})
            for _, path in refs:
                content.append({"type": "image_url", "image_url": {"url": data_url(path)}})
        content.append({"type": "text", "text": "Generate one 16:9 image: " + prompt})
        data = _request("/chat/completions", {
            "model": self.model,
            "modalities": ["image", "text"],
            "image_config": {"aspect_ratio": "16:9"},
            "messages": [{"role": "user", "content": content}],
        }, timeout=300)
        msg = data["choices"][0]["message"]
        images = msg.get("images") or []
        if not images:
            reason = (msg.get("content") or msg.get("refusal") or "no image returned").strip()
            raise ProviderError(f"No image: {reason[:300]}")
        url = images[0]["image_url"]["url"]
        head, b64 = url.split(",", 1)
        ext = "png" if "png" in head else "jpg"
        return base64.b64decode(b64), ext, (data.get("usage") or {}).get("cost") or 0


def still_provider(model):
    # Local Flux slots in here later, e.g. `if model.startswith("local/")`.
    return OpenRouterStills(model)


# --------------------------------------------------------------------- video

_video_models = {"at": 0, "data": {}}


def video_models():
    if time.time() - _video_models["at"] > 3600 or not _video_models["data"]:
        data = _request("/videos/models", timeout=30)["data"]
        _video_models["data"] = {m["id"]: m for m in data}
        _video_models["at"] = time.time()
    return _video_models["data"]


def video_durations(model):
    m = video_models().get(model) or {}
    return sorted(m.get("supported_durations") or range(1, 16))


def clip_seconds(model, length):
    """Shortest supported clip duration that covers length, or None if too long."""
    for d in video_durations(model):
        if d >= length - 0.05:
            return d
    return None


def clip_limit(model):
    return max(video_durations(model))


def video_price(model, resolution, seconds):
    """Estimated USD for one image-to-video clip, from the published price list.
    Returns None when the model is priced in a way we can't estimate."""
    skus = (video_models().get(model) or {}).get("pricing_skus") or {}
    per_sec = None
    for key in (f"cents_per_video_output_second_{resolution}", f"duration_seconds_{resolution}",
                f"image_to_video_duration_seconds_{resolution}", "cents_per_second_output", "duration_seconds"):
        if key in skus:
            v = float(skus[key])
            per_sec = v / 100 if key.startswith("cents") else v
            break
    if per_sec is None:
        return None
    image = float(skus.get("cents_per_image_input", 0)) / 100
    return round(per_sec * seconds + image, 4)


def submit_video(model, prompt, still_path, seconds, resolution):
    return _request("/videos", {
        "model": model,
        "prompt": prompt,
        "duration": int(seconds),
        "resolution": resolution,
        "aspect_ratio": "16:9",
        "generate_audio": False,
        "frame_images": [{"type": "image_url", "image_url": {"url": data_url(still_path)},
                          "frame_type": "first_frame"}],
    }, timeout=120)


def poll_video(job_id):
    return _request(f"/videos/{job_id}", timeout=60)


def download_video(job):
    url = (job.get("unsigned_urls") or [None])[0] or f"{BASE}/videos/{job['id']}/content?index=0"
    return _request(url, raw=True, timeout=300)
