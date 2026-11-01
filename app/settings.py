"""App-wide settings (not per song), kept in settings.json at the repo root.

blender: path to the Blender binary used to write music_video.blend at export.
Empty means no .blend; the MP4 and lyrics.srt are made either way.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path

from songs import ROOT

FILE = ROOT / "settings.json"
DEFAULTS = {"blender": ""}


def get():
    try:
        data = json.loads(FILE.read_text())
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def save(patch):
    data = {**get(), **{k: v for k, v in patch.items() if k in DEFAULTS}}
    FILE.write_text(json.dumps(data, indent=2) + "\n")
    return data


def blender():
    """The saved Blender path, or None when it isn't set or no longer exists."""
    p = get()["blender"].strip()
    return p if p and Path(p).is_file() else None


# ------------------------------------------------------------- find / test

def _version(path):
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"Blender\s+(\d+\.\d+(?:\.\d+)?)", r.stdout)
    return m.group(1) if m else None


def find_blenders():
    """Blender installs this machine has in the usual places, newest first."""
    home = Path.home()
    patterns = [
        os.environ.get("BLENDER", ""), shutil.which("blender") or "",
        "/opt/blender*/blender", str(home / "blender*/blender"), str(home / "Applications/blender*/blender"),
        str(home / "*/blender-*/blender"), "/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender",
        "/Applications/Blender*.app/Contents/MacOS/Blender",
        "C:/Program Files/Blender Foundation/Blender*/blender.exe",
    ]
    seen, found = set(), []
    for pat in patterns:
        for p in sorted(glob.glob(pat)) if pat else []:
            real = os.path.realpath(p)
            if real in seen or not os.path.isfile(real) or not os.access(real, os.X_OK):
                continue
            seen.add(real)
            v = _version(p)
            if v:
                found.append({"path": p, "version": v})
    found.sort(key=lambda f: [int(x) for x in f["version"].split(".")], reverse=True)
    return found


def test_blender(path, ffmpeg):
    """Write a tiny real project with mv/blend.py: a 2 s silent song, one black
    clip and a background, exactly as export does. Returns ok/version/error."""
    path = (path or "").strip()
    if not path:
        return {"ok": False, "error": "Enter the path to the Blender program first."}
    if not Path(path).is_file():
        return {"ok": False, "error": f"No file at {path}."}
    version = _version(path)
    if not version:
        return {"ok": False, "error": "That file didn't answer `--version` like Blender does."}
    t = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "clips").mkdir()
        (d / "clean").mkdir()
        with wave.open(str(d / "song.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"\0\0" * 32000)
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=black:s=320x180:r=24:d=2",
                        "-pix_fmt", "yuv420p", str(d / "clips" / "test.mp4")], check=True)
        shutil.copy(d / "clips" / "test.mp4", d / "clean" / "test.mp4")
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=black:s=320x180",
                        "-frames:v", "1", str(d / "gap.png")], check=True)
        (d / "plan.json").write_text(json.dumps([{"id": "test", "start": 0, "end": 2, "text": "Test"}]))
        try:
            r = subprocess.run([path, "-b", "--factory-startup", "--python", str(ROOT / "mv" / "blend.py"),
                                "--", str(d), str(d / "gap.png")], capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return {"ok": False, "version": version, "error": "Blender took over 3 minutes and was stopped."}
        if r.returncode != 0 or not (d / "music_video.blend").exists():
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-6:]
            return {"ok": False, "version": version,
                    "error": "Blender ran but couldn't write the project:\n" + "\n".join(tail)}
    return {"ok": True, "version": version, "seconds": round(time.time() - t, 1)}
