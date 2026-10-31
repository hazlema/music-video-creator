"""Shared helpers for the music video pipeline."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv():
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.removeprefix("export ").split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def api_key():
    load_dotenv()
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("Set OPENROUTER_API_KEY (env or .env).")
    return key


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def timecode(t):
    """15.26 -> '00-15.26' (sorts correctly, safe in filenames)."""
    return f"{int(t // 60):02d}-{t % 60:05.2f}"


def fmt(t):
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def ffmpeg():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()
