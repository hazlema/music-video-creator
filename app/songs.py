"""A song is a folder under songs/, laid out like media/ so the mv/ scripts
can still read it:

    song.wav  lyrics.txt  brief.txt  transcript.json  lines.json  plan.json
    song.json      title, style line, global references, model settings, spend
    jobs.json      every paid video request, written before it is sent
    refs/          global and section-only reference images
    stills/        <uid>-v<n>.<ext>, every still ever made
    raw/           <uid>-c<n>.mp4, every clip ever made
    export/        rendered output

plan.json stays a list of sections in the mv/ format (id, i, text, start, end,
duration, image_prompt, video_prompt) with the app's fields added.
"""
import json
import math
import re
import secrets
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SONGS = ROOT / "songs"

DEFAULTS = {
    "title": "",
    "style": "",
    "refs": [],
    "transcribe_model": "elevenlabs/scribe-v2",
    "llm_model": "anthropic/claude-sonnet-5.5",
    "image_model": "google/gemini-nano-banana-2.1",
    "video_model": "x-ai/grok-imagine-video-1.5",
    "video_resolution": "480p",
    "timing_locked": True,   # section edges can't be dragged until unticked
    "spend": [],
}


def uid():
    return secrets.token_hex(4)


def timecode(t):
    return f"{int(t // 60):02d}-{t % 60:05.2f}"


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def write_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def slugify(title):
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
    return s or "song"


def new_section(start, end, kind="lyric", **kw):
    s = {
        "uid": uid(), "kind": kind, "static": kind == "gap",
        "start": round(start, 2), "end": round(end, 2),
        "beat": "", "image_prompt": "", "video_prompt": "", "extra": "",
        "edited": {}, "refs": {"off": [], "add": []},
        "locked": False, "approved": False,
        "stills": [], "still": None, "clips": [], "clip": None,
        "failed": None, "use_stale": False,
    }
    s.update(kw)
    return s


def still_costs():
    """Average cost of one still per image model, from every song's history."""
    seen = {}
    for plan in SONGS.glob("*/plan.json"):
        for sec in read_json(plan, []) or []:
            for v in sec.get("stills", []):
                if v.get("cost") and v.get("model"):
                    seen.setdefault(v["model"], []).append(v["cost"])
    return {m: round(sum(c) / len(c), 4) for m, c in seen.items()}


class Song:
    def __init__(self, slug):
        self.slug = slug
        self.dir = SONGS / slug
        self.lock = threading.RLock()
        self.meta = {**DEFAULTS, **(read_json(self.dir / "song.json", {}) or {})}
        self.plan = read_json(self.dir / "plan.json", []) or []
        self.lines = read_json(self.dir / "lines.json", []) or []
        self.jobs = read_json(self.dir / "jobs.json", []) or []
        self._length = None

    # ------------------------------------------------------------- files
    def text(self, name):
        p = self.dir / name
        return p.read_text() if p.exists() else ""

    def set_text(self, name, value):
        (self.dir / name).write_text(value)

    @property
    def length(self):
        if self._length is None:
            try:
                with wave.open(str(self.dir / "song.wav")) as w:
                    self._length = w.getnframes() / w.getframerate()
            except (FileNotFoundError, wave.Error, EOFError):
                self._length = 0.0
        return self._length

    def reset_length(self):
        self._length = None

    # -------------------------------------------------------------- save
    def save_meta(self):
        with self.lock:
            write_json(self.dir / "song.json", self.meta)

    def save_lines(self):
        with self.lock:
            write_json(self.dir / "lines.json", self.lines)

    def save_jobs(self):
        with self.lock:
            write_json(self.dir / "jobs.json", self.jobs)

    def save_plan(self):
        with self.lock:
            self.plan.sort(key=lambda s: s["start"])
            for s in self.plan:
                # Keep the mv/ fields current for the CLI scripts.
                s["id"] = timecode(s["start"])
                s["duration"] = max(1, math.ceil(s["end"] - s["start"] - 1e-6))
                words = self.caption_text(s["start"], s["end"])
                s["text"] = words
                over = [l["i"] for l in self.lines if l["start"] < s["end"] and l["end"] > s["start"]]
                s["i"] = over[0] if over else None
            write_json(self.dir / "plan.json", self.plan)

    def caption_text(self, start, end):
        mid = [l["text"] for l in self.lines if start <= (l["start"] + l["end"]) / 2 < end]
        return " / ".join(mid)

    # ------------------------------------------------------------ lookup
    def section(self, sid):
        for s in self.plan:
            if s["uid"] == sid:
                return s
        raise KeyError(f"No section {sid}")

    def neighbours(self, s):
        k = self.plan.index(s)
        return (self.plan[k - 1] if k else None, self.plan[k + 1] if k + 1 < len(self.plan) else None)

    def ref(self, rid):
        for r in self.meta["refs"]:
            if r["id"] == rid:
                return r
        return None

    def cover(self):
        """A small JPEG of one of the song's stills for the library, made once
        per still. Uses the shot nearest a third of the way in: past the intro,
        usually showing the main characters."""
        with self.lock:
            shots = [s for s in self.plan if s.get("still") is not None]
            if not shots:
                return None
            mark = (self.plan[-1]["end"] if self.plan else 0) / 3
            s = min(shots, key=lambda s: abs((s["start"] + s["end"]) / 2 - mark))
            v = next((v for v in s["stills"] if v["n"] == s["still"]), None)
        if not v or not (self.dir / "stills" / v["file"]).exists():
            return None
        name = f"cover-{Path(v['file']).stem}.jpg"
        if not (self.dir / name).exists():
            try:
                from PIL import Image
            except ImportError:
                return None
            for old in self.dir.glob("cover-*.jpg"):
                old.unlink()
            im = Image.open(self.dir / "stills" / v["file"]).convert("RGB")
            im.thumbnail((480, 480))
            im.save(self.dir / name, "JPEG", quality=82)
        return name

    def add_spend(self, what, cost, uid=None):
        if not cost:
            return
        with self.lock:
            self.meta["spend"].append({"what": what, "cost": round(float(cost), 5), "uid": uid,
                                       "at": time.time()})
            self.save_meta()

    def stage(self):
        if not (self.dir / "song.wav").exists():
            return "Setup"
        if not self.lines:
            return "Transcribe"
        if not any(s["image_prompt"] for s in self.plan):
            return "Storyboard"
        if any(s["still"] is None for s in self.plan):
            return "Stills"
        if (self.dir / "export" / "music_video.mp4").exists():
            return "Exported"
        return "Animation"

    def spent(self):
        return round(sum(e["cost"] for e in self.meta["spend"]), 4)


_songs = {}
_songs_lock = threading.Lock()


def get(slug):
    with _songs_lock:
        if slug not in _songs:
            if not re.fullmatch(r"[a-z0-9-]+", slug) or not (SONGS / slug).is_dir():
                raise KeyError(f"No song {slug}")
            _songs[slug] = Song(slug)
        return _songs[slug]


def create(title):
    SONGS.mkdir(exist_ok=True)
    base = slugify(title)
    slug, n = base, 2
    while (SONGS / slug).exists():
        slug, n = f"{base}-{n}", n + 1
    d = SONGS / slug
    for sub in ("refs", "stills", "raw", "export"):
        (d / sub).mkdir(parents=True)
    write_json(d / "song.json", {**DEFAULTS, "title": title})
    return get(slug)


def library():
    SONGS.mkdir(exist_ok=True)
    out = []
    for d in sorted(SONGS.iterdir()):
        if d.is_dir() and (d / "song.json").exists():
            s = get(d.name)
            out.append({"slug": s.slug, "title": s.meta["title"] or s.slug, "stage": s.stage(),
                        "spent": s.spent(), "sections": len(s.plan), "length": round(s.length, 1),
                        "cover": s.cover()})
    return out
