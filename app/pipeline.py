"""The work behind each button: transcribe, align, storyboard, stills, clips,
export. Long steps run as background tasks the page polls."""
import io
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import wave
from pathlib import Path

import numpy as np

import providers
from providers import ProviderError
from songs import ROOT, new_section, read_json, timecode, uid, write_json

sys.path.insert(0, str(ROOT / "mv"))
import align as mv_align  # noqa: E402
from transcribe import downsample_16k_mono, word_segments  # noqa: E402

LOW_CONFIDENCE = 0.6
BRIDGE_GAP = 2.0     # a lyric shot runs on to the next line when the gap is this short
TAIL = 0.75          # otherwise it ends this long after its line
MIN_GAP = 1.0        # shorter holes are absorbed by the previous shot
MIN_SHOT = 1.0       # shorter shots are folded into a neighbour
MARKER = re.compile(r"^\s*[\[{](.*?)[\]}]\s*$")   # [Chorus], {Bridge}: structure, not sung
CLIP_WORKERS = 4
W, H, FPS = 854, 480, 24
BLENDER = Path("/opt/blender-5.1.2-linux-x64/blender")
NO_TEXT = " No text, no words, no lettering anywhere in the image."


def ffmpeg():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


# ===================================================================== tasks

class Tasks:
    """In-memory progress for everything running in the background."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}

    def start(self, song, kind, fn, uid=None, label=""):
        tid = f"t{int(time.time() * 1000)}{len(self.items)}"
        t = {"tid": tid, "song": song.slug, "kind": kind, "uid": uid, "label": label,
             "state": "running", "msg": "", "progress": None, "at": time.time()}
        with self.lock:
            self.items[tid] = t

        def run():
            try:
                fn(t)
                t["state"] = "done"
            except Exception as e:  # surfaced in the page
                if not isinstance(e, (ProviderError, ValueError)):
                    traceback.print_exc()
                t["state"], t["msg"] = "failed", str(e)
            t["ended"] = time.time()

        threading.Thread(target=run, daemon=True).start()
        return t

    def for_song(self, slug):
        now = time.time()
        with self.lock:
            for k in [k for k, t in self.items.items() if t.get("ended") and now - t["ended"] > 120]:
                del self.items[k]
            return [dict(t) for t in self.items.values() if t["song"] == slug]

    def busy(self, slug, kind, uid=None):
        with self.lock:
            return any(t["song"] == slug and t["kind"] == kind and t["state"] == "running"
                       and (uid is None or t["uid"] == uid) for t in self.items.values())


tasks = Tasks()


# ================================================================== waveform

def read_wav_mono(path):
    with wave.open(str(path)) as w:
        ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        x = ((b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
             << 8 >> 8).astype(np.float32) / 8388608
    elif width == 4:
        x = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648
    else:
        raise ValueError(f"Unsupported WAV sample width {width}")
    return x.reshape(-1, ch).mean(axis=1), rate


def peaks(song, per_second=100):
    cache = song.dir / "peaks.json"
    data = read_json(cache)
    if data and data.get("per_second") == per_second:
        return data
    x, rate = read_wav_mono(song.dir / "song.wav")
    step = max(1, rate // per_second)
    n = len(x) // step
    blocks = np.abs(x[: n * step]).reshape(n, step).max(axis=1)
    top = float(np.percentile(blocks, 99.5)) or 1.0
    data = {"per_second": rate / step, "peaks": [round(min(1.0, float(v) / top), 3) for v in blocks]}
    write_json(cache, data)
    return data


def import_audio(song, filename, payload):
    """Store an upload as song.wav (16-bit PCM), converting non-WAV files."""
    dest = song.dir / "song.wav"
    if filename.lower().endswith(".wav"):
        try:
            with wave.open(io.BytesIO(payload)) as w:
                ok = w.getsampwidth() in (2, 3, 4)
        except wave.Error:
            ok = False
        if ok:
            dest.write_bytes(payload)
            _after_audio(song)
            return
    with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix or ".bin") as tmp:
        tmp.write(payload)
        tmp.flush()
        subprocess.run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", tmp.name,
                        "-acodec", "pcm_s16le", "-ar", "44100", str(dest)], check=True)
    _after_audio(song)


def _after_audio(song):
    (song.dir / "peaks.json").unlink(missing_ok=True)
    song.reset_length()


# =========================================================== transcribe/align

COARSE = 20.0   # a transcript phrase longer than this carries no usable timing
PIECE = 4.0     # seconds per piece when re-transcribing for timing


def transcribe(song, task):
    model = song.meta.get("transcribe_model") or "elevenlabs/scribe-v2"
    task["msg"] = f"Sending the song to {model}"
    wav = downsample_16k_mono(song.dir / "song.wav")
    data, cost = providers.transcribe(wav, model)
    song.add_spend("Transcription", cost)
    segs = word_segments(data)
    if segs:   # per-word times: each sung word becomes its own segment
        data["phrases"], data["segments"] = data.get("segments"), segs
    # Without word times a song can come back as one phrase spanning the whole
    # track, which would spread the words evenly from 0:00 and put lyrics over
    # the intro. Then transcribe short pieces instead: each piece's words land
    # in its window, and pieces with no singing come back empty or as filler
    # that isn't in the lyrics.
    elif any(seg["end"] - seg["start"] > COARSE for seg in data.get("segments") or []):
        task["msg"] = "No timing came back; transcribing in short pieces to find where the words are"
        data["segments"], cost = transcribe_pieces(wav, model)
        data["pieces"] = PIECE
        song.add_spend("Transcription (timing pieces)", cost)
    write_json(song.dir / "transcript.json", data)
    task["msg"] = "Matching to your lyrics"
    align(song)


def transcribe_pieces(wav, model):
    """Transcribe a 16 kHz WAV in PIECE-second windows; segments in song time."""
    from concurrent.futures import ThreadPoolExecutor

    with wave.open(io.BytesIO(wav)) as r:
        params, rate, n = r.getparams(), r.getframerate(), r.getnframes()
        frames = r.readframes(n)
        bpf = r.getsampwidth() * r.getnchannels()
    length = n / rate

    def one(t0):
        t1 = min(t0 + PIECE, length)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setparams(params)
            w.writeframes(frames[int(t0 * rate) * bpf: int(t1 * rate) * bpf])
        d, c = providers.transcribe(buf.getvalue(), model)
        text = " ".join(seg["text"].strip() for seg in d.get("segments") or []).strip()
        return {"start": round(t0, 3), "end": round(t1, 3), "text": text}, c

    starts = [i * PIECE for i in range(int(length // PIECE) + 1) if i * PIECE < length - 0.2]
    with ThreadPoolExecutor(8) as ex:
        got = list(ex.map(one, starts))
    return [seg for seg, _ in got if seg["text"]], sum(c for _, c in got)


def align(song):
    """Time the pasted lyrics from transcript.json. Real lyrics always win."""
    segments = read_json(song.dir / "transcript.json", {}).get("segments") or []
    # Section markers like [Chorus] aren't sung: they label the next line
    # instead of becoming a caption.
    lyric_lines, parts, label = [], [], None
    for l in song.text("lyrics.txt").splitlines():
        l = l.strip()
        m = MARKER.match(l)
        if m:
            label = m.group(1).strip() or None
        elif l:
            lyric_lines.append(l)
            parts.append(label)
            label = None
    if not segments:
        raise ValueError("Transcribe the song first.")
    if not lyric_lines:
        raise ValueError("Paste the lyrics first.")

    tw = mv_align.transcript_words(segments)
    lw, owner, raw = [], [], []
    for li, line in enumerate(lyric_lines):
        for w in line.split():
            if mv_align.norm(w):
                lw.append(mv_align.norm(w))
                raw.append(w)
                owner.append(li)
    pairs = mv_align.align(lw, tw)
    starts = [tw[pairs[k]]["start"] if k in pairs else None for k in range(len(lw))]
    ends = [tw[pairs[k]]["end"] if k in pairs else None for k in range(len(lw))]
    word_dur = sorted(w["end"] - w["start"] for w in tw)[len(tw) // 2] if tw else 0.4
    mv_align.interpolate(starts, pairs.keys(), word_dur)
    mv_align.interpolate(ends, pairs.keys(), word_dur)

    lines, prev_end = [], 0.0
    for li, text in enumerate(lyric_lines):
        ks = [k for k in range(len(lw)) if owner[k] == li]
        if not ks:
            continue
        matched = sum(1 for k in ks if k in pairs)
        start = max(starts[ks[0]], prev_end)
        end = max(ends[ks[-1]], start + 0.5)
        prev_end = end
        words = [{"w": raw[k], "start": round(max(starts[k], start), 2), "end": round(min(ends[k], end), 2)}
                 for k in ks]
        conf = matched / len(ks)
        lines.append({"i": len(lines), "text": text, "part": parts[li], "start": round(start, 2), "end": round(end, 2),
                      "matched": f"{matched}/{len(ks)}", "confidence": round(conf, 2),
                      "low": conf < LOW_CONFIDENCE, "confirmed": False, "words": words})
    with song.lock:
        song.lines = lines
        song.save_lines()
        if not any(s["stills"] or s["locked"] for s in song.plan):
            song.plan = initial_sections(lines, song.length)
            song.save_plan()


def initial_sections(lines, length):
    """One shot per lyric line, dashed gap sections for instrumental stretches."""
    shots = []
    for k, line in enumerate(lines):
        nxt = lines[k + 1]["start"] if k + 1 < len(lines) else None
        if nxt is not None and nxt - line["end"] <= BRIDGE_GAP:
            end = nxt
        else:
            end = line["end"] + TAIL
            if nxt is not None:
                end = min(end, nxt)
        shots.append(new_section(line["start"], end))
    if length:
        shots[-1]["end"] = min(shots[-1]["end"], round(length, 2))

    # A shot a fraction of a second long is useless: give its time to the previous shot.
    merged = []
    for s in shots:
        if merged and s["end"] - s["start"] < MIN_SHOT and abs(merged[-1]["end"] - s["start"]) < 0.01:
            merged[-1]["end"] = s["end"]
        elif merged and merged[-1]["end"] - merged[-1]["start"] < MIN_SHOT:
            s["start"] = merged[-1]["start"]
            merged[-1] = s
        else:
            merged.append(s)
    shots = merged

    out, cursor = [], 0.0
    for s in shots:
        hole = s["start"] - cursor
        if hole >= MIN_GAP:
            out.append(new_section(cursor, s["start"], "gap"))
        elif out and hole > 0:
            out[-1]["end"] = s["start"]
        elif hole > 0:
            s["start"] = round(cursor, 2)
        out.append(s)
        cursor = s["end"]
    if length and length - cursor >= MIN_GAP:
        out.append(new_section(cursor, length, "gap"))
    elif out and length:
        out[-1]["end"] = round(length, 2)
    return out


# ================================================================ storyboard

SYSTEM = """You are a music video director writing a shot list for a whole song.
You get the creator's brief and every section of the song with its timing and lyrics. Some sections are instrumental gaps.

First decide one song-specific visual rule that follows from the brief (for example "Never leave the rehearsal room; impossible weather shows the singer's feelings"). Apply it throughout instead of defaulting to a journey, a spectacle chorus and a triumphant ending. Turn the brief into a coherent video; don't illustrate every lyric literally. Performance or abstract videos are equally valid if the brief points there.

For every section write:
- beat: one short sentence of creative intention, what the viewer should see or feel (e.g. "The stage becomes a cage.").
- scene: the image prompt for the section's still, 60-120 words. It serves the beat.
- motion: what moves and how the camera moves during the clip, 20-50 words.

Stills are generated independently by an image model and then animated by a video model, so:
- Every scene must be fully self-contained: restate the setting, the characters' appearance and the look each time. Never write "same as before".
- Describe recurring characters identically every time. Named references (listed below) are given to the image model as pictures; refer to them by name.
- When lyrics return (a chorus), keep the same visual motif but change framing, action or the motif's state to match where the song is emotionally. Don't repeat shots exactly.
- Interpret lyrics through the brief.
- Never put text, words, captions, signage lettering or logos in any shot.
Reply with JSON only, no prose:
{"rule": "<the visual rule>", "sections": [{"k": <section number>, "beat": "...", "scene": "...", "motion": "..."}]}"""

ONE_SYSTEM = """You are a music video director revising one shot of a storyboard.
You get the creator's brief, the video's visual rule, the neighbouring shots for continuity, and the shot to revise with its story beat.
Write new prompts that serve the beat. The scene (60-120 words) must be self-contained: restate setting, characters' appearance and look. The motion (20-50 words) says what moves and how the camera moves. Never put text, words or logos in the shot.
Reply with JSON only: {"beat": "<keep the given beat unless asked to write one>", "scene": "...", "motion": "..."}"""


def fmt(t):
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def _context(song):
    refs = [r for r in song.meta["refs"]]
    ref_text = "\n".join(f"- {r['label']}" for r in refs) or "(none)"
    return (f"BRIEF:\n{song.text('brief.txt').strip() or '(no brief)'}\n\n"
            f"STYLE LINE (added to every image prompt automatically, don't repeat it): {song.meta['style'] or '(none)'}\n\n"
            f"NAMED REFERENCES:\n{ref_text}\n")


def _section_row(song, k, s):
    words = song.caption_text(s["start"], s["end"])
    what = f'lyrics: "{words}"' if words else "instrumental (no lyrics)"
    labels = [l["part"] for l in song.lines if l.get("part") and s["start"] <= (l["start"] + l["end"]) / 2 < s["end"]]
    part = f" ({', '.join(labels)} starts here)" if labels else ""
    return f"{k}. [{fmt(s['start'])}-{fmt(s['end'])}]{part} {what}"


def storyboard(song, task):
    with song.lock:
        rows = "\n".join(_section_row(song, k, s) for k, s in enumerate(song.plan))
    task["msg"] = f"Writing {len(song.plan)} shots with {song.meta['llm_model']}"
    data, cost = providers.chat_json(song.meta["llm_model"], SYSTEM, f"{_context(song)}\nSECTIONS:\n{rows}")
    song.add_spend("Storyboard", cost)
    by_k = {int(x["k"]): x for x in data.get("sections", [])}
    kept, missing = 0, 0
    with song.lock:
        song.meta["rule"] = data.get("rule", "")
        song.save_meta()
        for k, s in enumerate(song.plan):
            got = by_k.get(k)
            if not got:
                missing += 1
                continue
            if s["locked"]:
                continue
            for field, key in (("beat", "beat"), ("image_prompt", "scene"), ("video_prompt", "motion")):
                if s["edited"].get(field) and s[field]:
                    kept += 1
                    continue
                s[field] = got.get(key, "").strip()
        song.save_plan()
    notes = []
    if kept:
        notes.append(f"kept {kept} hand-edited fields")
    if missing:
        notes.append(f"{missing} sections came back empty")
    task["msg"] = "Done" + (": " + ", ".join(notes) if notes else "")


def propose(song, sid, write_beat=False):
    """New prompts for one section from its beat. Saves nothing."""
    with song.lock:
        s = song.section(sid)
        prev, nxt = song.neighbours(s)
        near = "\n".join(f"{label}: beat: {n['beat']}\n  scene: {n['image_prompt']}"
                         for label, n in (("Previous shot", prev), ("Next shot", nxt)) if n)
        row = _section_row(song, song.plan.index(s), s)
    ask = ("Write a fresh beat too." if write_beat or not s["beat"]
           else f"Story beat (keep it): {s['beat']}")
    user = (f"{_context(song)}\nVISUAL RULE: {song.meta.get('rule') or '(none)'}\n\n{near}\n\n"
            f"SHOT TO REVISE: {row}\n{ask}\nCurrent scene: {s['image_prompt']}\nCurrent motion: {s['video_prompt']}")
    data, cost = providers.chat_json(song.meta["llm_model"], ONE_SYSTEM, user, max_tokens=2000)
    song.add_spend("Prompt rewrite", cost, sid)
    return {"beat": data.get("beat", s["beat"]).strip(), "image_prompt": data.get("scene", "").strip(),
            "video_prompt": data.get("motion", "").strip()}


CHANGE_SYSTEM = """You edit storyboard prompts for a music video at the creator's request.
You get the creator's brief, the video's visual rule, the shots just outside the selection for continuity, and the shots to edit.
Each shot has a scene prompt (the still image), a motion prompt (how the still is animated) and sometimes extra instructions.

Apply the creator's request with the smallest edit that carries it out. Keep every other word, detail and phrase unless it contradicts the change; don't polish, reorder or tighten anything else. Fold any extra instructions into the scene or motion the same way, so nothing is left outside the prompts.
Change the motion prompt only where the request affects it. Static shots have no motion: return their motion unchanged.
If the request doesn't apply to a shot, return that shot unchanged.
If the request conflicts with the visual rule or the brief, still apply it to these shots and say so in "warning" (one sentence), so the creator can decide.
Never put text, words or logos in the shot, and don't repeat the style line.
Reply with JSON only: {"shots": [{"n": <shot number>, "scene": "...", "motion": "...", "note": "<a few words on what changed, or 'no change'>"}], "warning": ""}"""

POLISH_SYSTEM = """You polish storyboard prompts for a music video.
You get the creator's brief, the video's visual rule, the shots just outside the selection for continuity, and the shots to polish.
Each shot has a scene prompt (the still image), a motion prompt (how the still is animated) and sometimes extra instructions.

Rewrite each scene (60-120 words) so an image model renders it well: concrete subjects and setting, appearance, lighting, framing. Rewrite each motion (20-50 words): what moves and how the camera moves. Keep every fact already there: characters, objects, colours, places, actions. Don't add new story. Fold any extra instructions in.
If the creator adds a request, apply it as well. If it conflicts with the visual rule or the brief, still apply it and say so in "warning" (one sentence).
Static shots have no motion: return their motion unchanged.
Never put text, words or logos in the shot, and don't repeat the style line.
Reply with JSON only: {"shots": [{"n": <shot number>, "scene": "...", "motion": "...", "note": "<a few words on what changed>"}], "warning": ""}"""

MAX_CHANGE = 30


def change(song, uids, text, polish=False):
    """Proposed prompt edits for the given sections from a plain-language request.
    Saves nothing; the page shows a diff and the creator accepts or not."""
    text = text.strip()
    with song.lock:
        chosen = [s for s in song.plan if s["uid"] in set(uids) and not s["locked"]]
        if not chosen:
            raise ValueError("Nothing to change: the selected sections are locked.")
        if len(chosen) > MAX_CHANGE:
            raise ValueError(f"Select at most {MAX_CHANGE} sections at a time.")
        idx = [song.plan.index(s) for s in chosen]
        before = song.plan[idx[0] - 1] if idx[0] > 0 and song.plan[idx[0] - 1] not in chosen else None
        after = song.plan[idx[-1] + 1] if idx[-1] + 1 < len(song.plan) and song.plan[idx[-1] + 1] not in chosen else None
        near = "\n".join(f"{label}: {n['image_prompt']}" for label, n in
                         (("Shot before the selection", before), ("Shot after the selection", after)) if n)
        rows = []
        for k, s in zip(idx, chosen):
            row = (f"{_section_row(song, k, s)}{' [static: no motion]' if s['static'] else ''}\n"
                   f"  beat: {s['beat'] or '(none)'}\n  scene: {s['image_prompt']}\n  motion: {s['video_prompt'] or '(none)'}")
            if s["extra"].strip():
                row += f"\n  extra instructions: {s['extra'].strip()}"
            rows.append(row)
    if polish:
        ask = f"Polish these shots.{' The creator also asks: ' + text if text else ''}"
    else:
        if not text:
            raise ValueError("Say what you want changed.")
        ask = f"The creator asks: {text}"
    user = (f"{_context(song)}\nVISUAL RULE: {song.meta.get('rule') or '(none)'}\n\n{near or '(no neighbouring shots)'}\n\n"
            f"SHOTS TO EDIT:\n" + "\n\n".join(rows) + f"\n\n{ask}")
    system = POLISH_SYSTEM if polish else CHANGE_SYSTEM
    for attempt in (1, 2):   # one retry if the reply doesn't parse
        try:
            data, cost = providers.chat_json(song.meta["llm_model"], system, user, max_tokens=1500 + 600 * len(chosen))
            break
        except providers.ProviderError as e:
            if attempt == 2 or "JSON" not in str(e):
                raise
    song.add_spend("Polish prompts" if polish else "Prompt change", cost, chosen[0]["uid"] if len(chosen) == 1 else None)
    shots = [r for r in data.get("shots") or [] if isinstance(r, dict)]
    got = {}
    for r in shots:
        m = re.search(r"\d+", str(r.get("n", "")))
        if m:
            got[int(m.group(0))] = r
    if not any(k in got for k in idx) and len(shots) == len(chosen):
        got = dict(zip(idx, shots))   # numbered differently, but one per shot in order
    out = []
    for k, s in zip(idx, chosen):
        r = got.get(k)
        if not r:
            continue
        scene = (r.get("scene") or "").strip() or s["image_prompt"]
        motion = s["video_prompt"] if s["static"] else (r.get("motion") or "").strip() or s["video_prompt"]
        out.append({"uid": s["uid"], "image_prompt": scene, "video_prompt": motion,
                    "fold_extra": bool(s["extra"].strip()), "note": (r.get("note") or "").strip()})
    if not out:
        raise providers.ProviderError("The storyboard model didn't return any shots. Try again.")
    return {"rows": out, "warning": (data.get("warning") or "").strip(), "cost": cost}


# ==================================================================== stills

def still_refs(song, s):
    out = []
    for r in song.meta["refs"]:
        if r["id"] not in s["refs"]["off"]:
            out.append((r["label"], song.dir / "refs" / r["file"]))
    for r in s["refs"]["add"]:
        out.append((r["label"], song.dir / "refs" / r["file"]))
    return out


def still_prompt(song, s):
    parts = [s["image_prompt"].strip()]
    if s["extra"].strip():
        parts.append(s["extra"].strip())
    if song.meta["style"].strip():
        parts.append("Style: " + song.meta["style"].strip())
    return " ".join(parts) + NO_TEXT


_still_slots = threading.Semaphore(4)


def make_still(song, sid, task, model=None):
    """model overrides the song's Stills model for this one still ("Regenerate with")."""
    with song.lock:
        s = song.section(sid)
        if not s["image_prompt"].strip():
            raise ValueError("This section has no image prompt yet. Run the storyboard or write one.")
        prompt = still_prompt(song, s)
        refs = still_refs(song, s)
        model = model or song.meta["image_model"]
    task["msg"] = "Waiting for a slot"
    with _still_slots:
        task["msg"] = f"Generating with {model}"
        try:
            img, ext, cost = providers.still_provider(model).generate(prompt, refs)
        except Exception as e:
            with song.lock:
                song.section(sid)["failed"] = {"what": "still", "reason": str(e)}
                song.save_plan()
            raise
    song.add_spend("Still", cost, sid)
    with song.lock:
        s = song.section(sid)
        n = max([v["n"] for v in s["stills"]], default=0) + 1
        name = f"{sid}-v{n}.{ext}"
        (song.dir / "stills" / name).write_bytes(img)
        s["stills"].append({"n": n, "file": name, "prompt": prompt, "model": model, "cost": cost,
                            "at": time.time()})
        select_still(s, n)
        song.save_plan()


def select_still(s, n):
    if s["still"] != n:
        s["still"] = n
        s["approved"] = False
    s["failed"] = None if (s["failed"] or {}).get("what") == "still" else s["failed"]


# ===================================================================== clips

_clip_slots = threading.Semaphore(CLIP_WORKERS)
ACTIVE = ("sending", "pending", "unknown")


def section_length(s):
    return s["end"] - s["start"]


def clip_status(song, s):
    """Why this section can or can't animate right now."""
    if s["static"]:
        return "static"
    if any(j["uid"] == s["uid"] and j["state"] in ACTIVE for j in song.jobs):
        job = next(j for j in song.jobs if j["uid"] == s["uid"] and j["state"] in ACTIVE)
        return "unknown" if job["state"] == "unknown" else "rendering"
    if s["still"] is None:
        return "no still"
    if not s["approved"]:
        return "draft"
    if providers.clip_seconds(song.meta["video_model"], section_length(s)) is None:
        return "split needed"
    return "ready"


def estimate(song, uids):
    model, res = song.meta["video_model"], song.meta["video_resolution"]
    rows = []
    with song.lock:
        for s in song.plan:
            if uids and s["uid"] not in uids:
                continue
            status = clip_status(song, s)
            secs = providers.clip_seconds(model, section_length(s))
            has_current = clip_current(s) is True
            rows.append({"uid": s["uid"], "id": timecode(s["start"]), "status": status,
                         "seconds": secs, "length": round(section_length(s), 2),
                         "has_current_clip": has_current,
                         "cost": providers.video_price(model, res, secs) if secs else None})
    return {"model": model, "resolution": res, "limit": providers.clip_limit(model), "rows": rows}


def _clip(s, n):
    return next((c for c in s["clips"] if c["n"] == n), {})


def clip_current(s):
    """None without a selected clip; False when the clip no longer matches its
    still or is too short for the (retimed) section."""
    c = _clip(s, s["clip"]) if s["clip"] is not None else None
    if not c:
        return None
    return c.get("still") == s["still"] and (c.get("seconds") or 99) + 0.05 >= section_length(s)


def animate(song, sid):
    """Queue one clip. The job is written to jobs.json before anything is sent."""
    with song.lock:
        s = song.section(sid)
        status = clip_status(song, s)
        if status != "ready":
            raise ValueError(f"Can't animate this section: {status}.")
        if not s["video_prompt"].strip():
            raise ValueError("This section has no motion prompt.")
        still = next(v for v in s["stills"] if v["n"] == s["still"])
        model, res = song.meta["video_model"], song.meta["video_resolution"]
        job = {"jid": uid(), "uid": sid, "still": s["still"], "still_file": still["file"],
               "prompt": s["video_prompt"].strip() + " No text, captions or words on screen.",
               "seconds": providers.clip_seconds(model, section_length(s)), "model": model,
               "resolution": res, "state": "queued", "provider_id": None, "error": None,
               "cost": None, "created": time.time()}
        song.jobs.append(job)
        song.save_jobs()
    threading.Thread(target=_run_clip, args=(song, job["jid"]), daemon=True).start()
    return job


def _job(song, jid):
    return next(j for j in song.jobs if j["jid"] == jid)


def _set_job(song, jid, **kw):
    with song.lock:
        _job(song, jid).update(kw)
        song.save_jobs()


def _run_clip(song, jid):
    with _clip_slots:
        job = _job(song, jid)
        if job["state"] == "queued":
            # A request is sent only once per local job id: mark it first, and
            # anything still 'sending' after a restart becomes 'unknown'.
            _set_job(song, jid, state="sending", sent_at=time.time())
            try:
                r = providers.submit_video(job["model"], job["prompt"], song.dir / "stills" / job["still_file"],
                                           job["seconds"], job["resolution"])
            except ProviderError as e:
                if str(e).startswith("HTTP"):
                    _set_job(song, jid, state="failed", error=str(e))
                    _mark_failed(song, job["uid"], str(e))
                else:
                    _set_job(song, jid, state="unknown", error=str(e))
                return
            except Exception as e:
                _set_job(song, jid, state="unknown", error=str(e))
                return
            _set_job(song, jid, state="pending", provider_id=r["id"])
        _poll_clip(song, jid)


def _poll_clip(song, jid):
    job = _job(song, jid)
    misses = 0
    while True:
        try:
            r = providers.poll_video(job["provider_id"])
            misses = 0
        except ProviderError as e:
            misses += 1
            if misses > 30:
                _set_job(song, jid, state="unknown", error=f"Lost track of the job: {e}")
                return
            time.sleep(20)
            continue
        if r["status"] == "completed":
            break
        if r["status"] in ("failed", "cancelled", "expired"):
            reason = r.get("error") or r["status"]
            _set_job(song, jid, state="failed", error=reason)
            _mark_failed(song, job["uid"], reason)
            return
        time.sleep(8)
    cost = (r.get("usage") or {}).get("cost")
    try:
        data = providers.download_video(r)
    except ProviderError as e:
        _set_job(song, jid, state="unknown", error=f"Finished but download failed: {e}")
        return
    with song.lock:
        try:
            s = song.section(job["uid"])
        except KeyError:  # section was merged away; keep the file anyway
            s = None
        n = max([c["n"] for c in s["clips"]], default=0) + 1 if s else 1
        name = f"{job['uid']}-c{n}.mp4"
        (song.dir / "raw" / name).write_bytes(data)
        if s:
            s["clips"].append({"n": n, "file": name, "still": job["still"], "seconds": job["seconds"],
                               "model": job["model"], "cost": cost, "at": time.time()})
            s["clip"] = n
            if (s["failed"] or {}).get("what") == "clip":
                s["failed"] = None
            s["use_stale"] = False
            song.save_plan()
        _job(song, jid).update(state="done", cost=cost, file=name)
        song.save_jobs()
    song.add_spend("Clip", cost, job["uid"])


def _mark_failed(song, sid, reason):
    with song.lock:
        try:
            song.section(sid)["failed"] = {"what": "clip", "reason": reason}
            song.save_plan()
        except KeyError:
            pass


def mark_job_failed(song, sid):
    with song.lock:
        for j in song.jobs:
            if j["uid"] == sid and j["state"] == "unknown":
                j["state"], j["error"] = "failed", "Marked failed by hand"
        song.save_jobs()


def resume_jobs(song):
    """On startup: resume polling sent jobs; a job caught mid-send is unknown."""
    with song.lock:
        for j in song.jobs:
            if j["state"] == "sending":
                j["state"], j["error"] = "unknown", "The app stopped while this request was being sent."
            elif j["state"] == "queued":
                j["state"], j["error"] = "failed", "The app stopped before this request was sent. Nothing was charged."
        song.save_jobs()
        pending = [j["jid"] for j in song.jobs if j["state"] == "pending"]
    for jid in pending:
        threading.Thread(target=lambda j=jid: (_clip_slots.acquire(), _poll_clip(song, j), _clip_slots.release()),
                         daemon=True).start()


# ==================================================================== export

def plan_export(song):
    """What each section will show in the export, and why when it's a fallback."""
    rows = []
    with song.lock:
        for s in song.plan:
            clip = _clip(s, s["clip"]) if s["clip"] is not None else None
            job_active = any(j["uid"] == s["uid"] and j["state"] in ACTIVE for j in song.jobs)
            reason = None
            if s["still"] is None:
                use, reason = "black", "no still"
            elif s["static"]:
                use = "still"
            elif job_active:
                use, reason = "still", "clip still rendering or unknown"
            elif (s["failed"] or {}).get("what") == "clip":
                use, reason = "still", "clip failed"
            elif not clip:
                use, reason = "still", "no clip"
            elif not s["approved"]:
                use, reason = "still", "section not approved"
            elif clip_current(s) is False and not s["use_stale"]:
                use, reason = "still", "clip out of date"
            else:
                use = "clip"
            rows.append({"uid": s["uid"], "id": timecode(s["start"]), "use": use, "reason": reason,
                         "stale": clip_current(s) is False})
    return rows


def _frames(t):
    return round(t * FPS)


def _ass_time(t):
    cs = max(0, round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _ass(lines, t0, t1):
    events = []
    for l in lines:
        a, b = max(l["start"], t0), min(l["end"], t1)
        if b - a < 0.05:
            continue
        fade = "{\\fad(250,200)}" if l["start"] >= t0 else ""
        text = l["text"].replace("{", "(").replace("}", ")")
        events.append(f"Dialogue: 0,{_ass_time(a - t0)},{_ass_time(b - t0)},Lyric,{fade}{text}")
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Lyric,DejaVu Sans,30,&H00FFFFFF,&H00000000,&H80000000,1,0,1,2,1,2,30,30,36

[Events]
Format: Layer, Start, End, Style, Text
""" + "\n".join(events) + "\n"


def _srt(lines):
    def t(x):
        ms = round(x * 1000)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return "\n".join(f"{n}\n{t(l['start'])} --> {t(l['end'])}\n{l['text']}\n" for n, l in enumerate(lines, 1))


def _render(src, kind, dest, dur, ass=None):
    frames = max(1, round(dur * FPS))
    base = [ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"]
    if kind == "clip":
        inp = ["-i", str(src)]
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},"
              f"tpad=stop_mode=clone:stop_duration={dur + 1},trim=end_frame={frames},setpts=PTS-STARTPTS")
    elif kind == "still":
        # Slow push-in so a held still doesn't look frozen.
        inp = ["-loop", "1", "-framerate", str(FPS), "-i", str(src)]
        vf = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
              f"zoompan=z='1+0.08*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},"
              f"setsar=1,trim=end_frame={frames},setpts=PTS-STARTPTS")
    else:
        inp = ["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={FPS}"]
        vf = f"setsar=1,trim=end_frame={frames},setpts=PTS-STARTPTS"
    if ass:
        vf += f",subtitles=filename='{ass}'"
    subprocess.run(base + inp + ["-vf", vf, "-frames:v", str(frames), "-an", "-r", str(FPS),
                                 "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-crf", "19",
                                 str(dest)], check=True)


def export(song, task, use_stale=()):
    with song.lock:
        for s in song.plan:
            s["use_stale"] = s["uid"] in use_stale
        song.save_plan()
        rows = {r["uid"]: r for r in plan_export(song)}
        plan = [dict(s) for s in song.plan]
        lines = [dict(l) for l in song.lines]
        length = song.length
    out = song.dir / "export"
    if out.exists():
        shutil.rmtree(out)
    for sub in ("clips", "clean"):
        (out / sub).mkdir(parents=True)

    # Cover the whole song: sections, plus black for any empty stretch.
    pieces, cursor = [], 0.0
    for s in plan:
        if s["start"] - cursor > 0.02:
            pieces.append({"id": timecode(cursor), "start": cursor, "end": s["start"], "use": "black"})
        pieces.append({"id": timecode(s["start"]), "start": s["start"], "end": s["end"], "use": rows[s["uid"]]["use"],
                       "s": s, "text": song.caption_text(s["start"], s["end"])})
        cursor = s["end"]
    if length - cursor > 0.02:
        pieces.append({"id": timecode(cursor), "start": cursor, "end": length, "use": "black"})

    with tempfile.TemporaryDirectory() as tmp:
        concat = []
        for n, p in enumerate(pieces, 1):
            task["msg"] = f"Rendering piece {n} of {len(pieces)}"
            task["progress"] = round(n / (len(pieces) + 2), 3)
            # Cut on frame boundaries so the pieces can't drift from the song.
            dur = (_frames(p["end"]) - _frames(p["start"])) / FPS
            if dur <= 0:
                continue
            src = None
            if p["use"] == "clip":
                s = p["s"]
                src = song.dir / "raw" / _clip(s, s["clip"])["file"]
            elif p["use"] == "still":
                s = p["s"]
                src = song.dir / "stills" / next(v for v in s["stills"] if v["n"] == s["still"])["file"]
            ass = Path(tmp) / f"{n}.ass"
            ass.write_text(_ass(lines, _frames(p["start"]) / FPS, _frames(p["end"]) / FPS))
            clean, captioned = out / "clean" / f"{p['id']}.mp4", out / "clips" / f"{p['id']}.mp4"
            _render(src, p["use"], clean, dur)
            _render(src, p["use"], captioned, dur, ass)
            concat.append(captioned)

        task["msg"] = "Joining pieces and adding the song"
        listing = Path(tmp) / "list.txt"
        listing.write_text("".join(f"file '{c}'\n" for c in concat))
        subprocess.run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(listing), "-i", str(song.dir / "song.wav"), "-map", "0:v", "-map", "1:a",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
                        str(out / "music_video.mp4")], check=True)

    (out / "lyrics.srt").write_text(_srt(lines))
    # mv/blend.py reads plan.json, song.wav, clips/ and clean/ from one folder.
    write_json(out / "plan.json", [{"id": p["id"], "start": p["start"], "end": p["end"],
                                    "text": p.get("text") or ""} for p in pieces])
    (out / "song.wav").symlink_to(song.dir / "song.wav")
    from PIL import Image
    Image.new("RGB", (W, H), "black").save(out / "gap.png")

    if BLENDER.exists():
        task["msg"] = "Writing music_video.blend"
        task["progress"] = round((len(pieces) + 1) / (len(pieces) + 2), 3)
        r = subprocess.run([str(BLENDER), "-b", "--factory-startup", "--python", str(ROOT / "mv" / "blend.py"),
                            "--", str(out), str(out / "gap.png")], capture_output=True, text=True)
        if r.returncode != 0 or not (out / "music_video.blend").exists():
            task["msg"] = "Video done; the .blend failed: " + (r.stderr or r.stdout)[-300:]
            return
    fallbacks = sum(1 for r in rows.values() if r["use"] != "clip")
    task["msg"] = f"Exported. {fallbacks} sections used stills."
