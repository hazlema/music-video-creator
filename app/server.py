"""Song -> music video, local web app.

Run:  uv run --with imageio-ffmpeg --with numpy --with pillow python app/server.py [--port 8780]
Then open http://localhost:8780
"""
import json
import mimetypes
import re
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline  # noqa: E402
import providers  # noqa: E402
import songs  # noqa: E402
from pipeline import tasks  # noqa: E402
from songs import new_section, uid  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}


class HttpError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


# ============================================================== song state

def section_view(song, s, low_lines, limit):
    jobs = [j for j in song.jobs if j["uid"] == s["uid"]]
    active = next((j for j in jobs if j["state"] in pipeline.ACTIVE), None)
    stale = pipeline.clip_current(s) is False
    split = not s["static"] and (s["end"] - s["start"]) > limit + 0.05
    low = any(l["start"] < s["end"] and l["end"] > s["start"] for l in low_lines)
    if s["failed"] or (active and active["state"] == "unknown"):
        status = "failed"
    elif stale:
        status = "stale"
    elif low:
        status = "check"
    elif split:
        status = "split"
    elif s["locked"]:
        status = "locked"
    else:
        status = "approved" if s["approved"] else "draft"
    return {**s, "status": status, "stale": stale, "split_needed": split, "low": low,
            "job": {k: active[k] for k in ("jid", "state", "error", "seconds", "created")} if active else None,
            "clip_count": len(s["clips"])}


def state(song):
    with song.lock:
        try:
            limit = providers.clip_limit(song.meta["video_model"])
        except Exception:
            limit = 15
        low_lines = [l for l in song.lines if l.get("low") and not l.get("confirmed")]
        return {
            "slug": song.slug, "meta": song.meta, "brief": song.text("brief.txt"),
            "lyrics": song.text("lyrics.txt"), "has_audio": (song.dir / "song.wav").exists(),
            "has_transcript": (song.dir / "transcript.json").exists(),
            "length": song.length, "lines": song.lines, "limit": limit,
            "plan": [section_view(song, s, low_lines, limit) for s in song.plan],
            "tasks": tasks.for_song(song.slug), "spent": song.spent(), "stage": song.stage(),
            "export": {f: (song.dir / "export" / f).exists() for f in ("music_video.mp4", "lyrics.srt",
                                                                       "music_video.blend")},
        }


def need_unlocked(*sections):
    locked = [s for s in sections if s["locked"]]
    if locked:
        raise HttpError(409, "Locked sections can't be changed. Unlock them first.")


def renumber(sections):
    """Merge version histories into one list with fresh numbers."""
    stills, clips, still_map, by_file, clip_files = [], [], {}, {}, set()
    for s in sections:
        for v in s["stills"]:
            # Parts of an earlier split share files; keep one entry per file.
            if v["file"] not in by_file:
                by_file[v["file"]] = len(stills) + 1
                stills.append({**v, "n": by_file[v["file"]]})
            still_map[(s["uid"], v["n"])] = by_file[v["file"]]
        for c in s["clips"]:
            if c["file"] in clip_files:
                continue
            clip_files.add(c["file"])
            clips.append({**c, "n": len(clips) + 1, "still": still_map.get((s["uid"], c.get("still")))})
    return stills, clips, still_map


# ================================================================== routes

def route(method, path, q, body, h):
    parts = [p for p in path.split("/") if p][1:]  # drop "api"
    if parts == ["songs"]:
        if method == "GET":
            return songs.library()
        if method == "POST":
            title = (body.get("title") or "").strip() or "Untitled song"
            return {"slug": songs.create(title).slug}
    if parts == ["models"] and method == "GET":
        try:
            catalog = providers.model_catalog()
        except providers.ProviderError:
            catalog = {"llm": [], "image": [{"id": i, "name": n, "local": True} for i, n in providers.LOCAL_STILLS.items()],
                       "video": [], "transcribe": []}   # offline: Setup falls back to typing an ID
        return {**catalog, "still_costs": songs.still_costs(), "sidecar": providers.sidecar_status(),
                "sidecar_help": providers.SIDECAR_HELP}
    if len(parts) < 2 or parts[0] != "songs":
        raise HttpError(404, "Not found")
    try:
        song = songs.get(parts[1])
    except KeyError as e:
        raise HttpError(404, str(e))
    rest = parts[2:]
    key = (method, rest[0] if rest else "")

    if key == ("GET", ""):
        return state(song)
    if key == ("GET", "peaks"):
        return pipeline.peaks(song)

    if key == ("PUT", "setup"):
        with song.lock:
            for f in ("title", "style", "transcribe_model", "llm_model", "image_model", "video_model", "video_resolution"):
                if f in body:
                    song.meta[f] = body[f].strip()
            if "timing_locked" in body:
                song.meta["timing_locked"] = bool(body["timing_locked"])
            song.save_meta()
            if "brief" in body:
                song.set_text("brief.txt", body["brief"])
            if "lyrics" in body:
                song.set_text("lyrics.txt", "\n".join(l.rstrip() for l in body["lyrics"].splitlines()).strip() + "\n")
        return state(song)

    if key == ("POST", "audio"):
        pipeline.import_audio(song, h.headers.get("X-Filename", "song.wav"), body)
        return state(song)

    if rest[:1] == ["refs"]:
        with song.lock:
            if method == "POST":
                ext = Path(h.headers.get("X-Filename", "ref.png")).suffix.lower()
                if ext not in IMAGE_EXT:
                    raise HttpError(400, "References must be PNG, JPG or WebP images.")
                rid = uid()
                (song.dir / "refs").mkdir(exist_ok=True)
                (song.dir / "refs" / f"{rid}{ext}").write_bytes(body)
                ref = {"id": rid, "file": f"{rid}{ext}",
                       "label": (h.headers.get("X-Label") or "").strip() or Path(h.headers.get("X-Filename", "reference")).stem}
                sid = q.get("section")
                if sid:
                    s = song.section(sid)
                    need_unlocked(s)
                    s["refs"]["add"].append(ref)
                    song.save_plan()
                else:
                    song.meta["refs"].append(ref)
                    song.save_meta()
                return state(song)
            rid = rest[1]
            if method == "PATCH":
                r = song.ref(rid)
                if r:
                    r["label"] = body.get("label", r["label"]).strip()
                    song.save_meta()
                for s in song.plan:
                    for r in s["refs"]["add"]:
                        if r["id"] == rid:
                            r["label"] = body.get("label", r["label"]).strip()
                song.save_plan()
                return state(song)
            if method == "DELETE":
                song.meta["refs"] = [r for r in song.meta["refs"] if r["id"] != rid]
                song.save_meta()
                for s in song.plan:
                    s["refs"]["add"] = [r for r in s["refs"]["add"] if r["id"] != rid]
                    s["refs"]["off"] = [x for x in s["refs"]["off"] if x != rid]
                song.save_plan()
                return state(song)

    if key == ("POST", "transcribe"):
        if not (song.dir / "song.wav").exists():
            raise HttpError(400, "Upload the song first.")
        if not song.text("lyrics.txt").strip():
            raise HttpError(400, "Paste the lyrics first. Transcription is only used for timing.")
        if tasks.busy(song.slug, "transcribe"):
            raise HttpError(409, "Already transcribing.")
        tasks.start(song, "transcribe", lambda t: pipeline.transcribe(song, t), label="Transcribe")
        return state(song)

    if key == ("POST", "align"):
        pipeline.align(song)
        return state(song)

    if rest[:1] == ["lines"] and method == "PATCH":
        with song.lock:
            i = int(rest[1])
            line = song.lines[i]
            prev = song.lines[i - 1] if i else None
            nxt = song.lines[i + 1] if i + 1 < len(song.lines) else None
            start = float(body.get("start", line["start"]))
            end = float(body.get("end", line["end"]))
            start = max(start, prev["end"] if prev else 0.0)
            end = min(end, nxt["start"] if nxt else song.length or end)
            if end - start < 0.2:
                raise HttpError(400, "A caption line needs at least 0.2 seconds.")
            line["start"], line["end"] = round(start, 2), round(end, 2)
            line["confirmed"] = bool(body.get("confirmed", True))
            song.save_lines()
            song.save_plan()  # section text follows the captions
        return state(song)

    if key == ("POST", "storyboard"):
        if not song.plan:
            raise HttpError(400, "Transcribe the song first so there are sections to plan.")
        if tasks.busy(song.slug, "storyboard"):
            raise HttpError(409, "The storyboard is already being written.")
        tasks.start(song, "storyboard", lambda t: pipeline.storyboard(song, t), label="Storyboard")
        return state(song)

    if key == ("POST", "stills"):
        with song.lock:
            wanted = body.get("uids") or [s["uid"] for s in song.plan if s["still"] is None]
            model = (body.get("model") or "").strip() or None   # "Regenerate with": just these stills
            chosen, skipped = [], []
            for sid in wanted:
                s = song.section(sid)
                if s["locked"]:
                    skipped.append(f"{songs.timecode(s['start'])} is locked")
                elif not s["image_prompt"].strip():
                    skipped.append(f"{songs.timecode(s['start'])} has no image prompt")
                elif tasks.busy(song.slug, "still", sid):
                    skipped.append(f"{songs.timecode(s['start'])} is already generating")
                else:
                    chosen.append(sid)
        for sid in chosen:
            tasks.start(song, "still", lambda t, sid=sid: pipeline.make_still(song, sid, t, model), uid=sid, label="Still")
        return {**state(song), "started": len(chosen), "skipped": skipped}

    if key == ("POST", "split"):
        with song.lock:
            s = song.section(body["uid"])
            need_unlocked(s)
            cuts = sorted(float(c) for c in body.get("cuts", []))
            cuts = [round(c, 2) for c in cuts if s["start"] + 0.2 < c < s["end"] - 0.2]
            if not cuts:
                raise HttpError(400, "Add at least one cut inside the section.")
            bounds = [s["start"], *cuts, s["end"]]
            k = song.plan.index(s)
            parts = []
            for a, b in zip(bounds, bounds[1:]):
                p = json.loads(json.dumps(s))
                p.update(uid=uid(), start=a, end=b, clip=None, use_stale=False, failed=None)
                parts.append(p)
            song.plan[k:k + 1] = parts
            song.save_plan()
        return state(song)

    if key == ("POST", "merge"):
        with song.lock:
            chosen = [s for s in song.plan if s["uid"] in set(body.get("uids", []))]
            if len(chosen) < 2:
                raise HttpError(400, "Select two or more adjacent sections to merge.")
            idx = [song.plan.index(s) for s in chosen]
            if idx != list(range(idx[0], idx[0] + len(idx))):
                raise HttpError(400, "Only adjacent sections can be merged.")
            need_unlocked(*chosen)
            first = chosen[0]
            stills, clips, still_map = renumber(chosen)
            merged = json.loads(json.dumps(first))
            merged.update(uid=uid(), end=chosen[-1]["end"], stills=stills, clips=clips, clip=None,
                          still=still_map.get((first["uid"], first["still"])), use_stale=False, failed=None,
                          static=all(s["static"] for s in chosen),
                          kind="lyric" if any(s["kind"] == "lyric" for s in chosen) else "gap")
            song.plan[idx[0]: idx[-1] + 1] = [merged]
            song.save_plan()
        return {**state(song), "selected": merged["uid"]}

    if key == ("POST", "retime"):
        if song.meta["timing_locked"]:
            raise HttpError(409, "Timing is locked. Untick Lock timing to drag section edges.")
        with song.lock:
            s = song.section(body["uid"])
            edge, t = body["edge"], round(float(body["t"]), 2)
            prev, nxt = song.neighbours(s)
            touching = (prev if edge == "start" else nxt)
            if touching and abs((touching["end"] if edge == "start" else touching["start"]) - s[edge]) > 0.02:
                touching = None
            need_unlocked(s, *([touching] if touching else []))
            if edge == "start":
                lo = touching["start"] + 0.3 if touching else (prev["end"] if prev else 0.0)
                t = min(max(t, lo), s["end"] - 0.3)
                s["start"] = t
                if touching:
                    touching["end"] = t
            else:
                hi = touching["end"] - 0.3 if touching else (nxt["start"] if nxt else song.length)
                t = max(min(t, hi), s["start"] + 0.3)
                s["end"] = t
                if touching:
                    touching["start"] = t
            song.save_plan()
        return state(song)

    if key == ("POST", "gap"):
        with song.lock:
            a, b = round(float(body["start"]), 2), round(float(body["end"]), 2)
            for s in song.plan:
                if s["start"] < b - 0.01 and s["end"] > a + 0.01:
                    a, b = (s["end"], b) if s["start"] <= a else (a, min(b, s["start"]))
            if b - a < 0.3:
                raise HttpError(400, "Drag across an empty stretch of the timeline to make a gap section.")
            g = new_section(a, b, "gap")
            song.plan.append(g)
            song.save_plan()
        return {**state(song), "selected": g["uid"]}

    if key == ("POST", "approve"):
        done, skipped = 0, []
        with song.lock:
            for sid in body.get("uids", []):
                s = song.section(sid)
                if s["locked"]:
                    skipped.append(f"{songs.timecode(s['start'])} is locked")
                elif s["still"] is None:
                    skipped.append(f"{songs.timecode(s['start'])} has no still")
                else:
                    s["approved"] = bool(body.get("approved", True))
                    done += 1
            song.save_plan()
        return {**state(song), "done": done, "skipped": skipped}

    if key == ("GET", "estimate"):
        uids = [u for u in (q.get("uids") or "").split(",") if u]
        return pipeline.estimate(song, uids)

    if key == ("POST", "animate"):
        started, skipped = 0, []
        for sid in body.get("uids", []):
            try:
                pipeline.animate(song, sid)
                started += 1
            except (ValueError, KeyError) as e:
                skipped.append(str(e))
        return {**state(song), "started": started, "skipped": skipped}

    if key == ("GET", "export-plan"):
        return {"rows": pipeline.plan_export(song)}

    if key == ("POST", "export"):
        if tasks.busy(song.slug, "export"):
            raise HttpError(409, "Already exporting.")
        if not song.plan:
            raise HttpError(400, "Nothing to export yet.")
        use_stale = set(body.get("use_stale", []))
        tasks.start(song, "export", lambda t: pipeline.export(song, t, use_stale), label="Export")
        return state(song)

    if key == ("POST", "change"):
        return pipeline.change(song, body.get("uids") or [], body.get("text") or "", bool(body.get("polish")))

    if rest[:1] == ["sections"]:
        with song.lock:
            s = song.section(rest[1])
            action = rest[2] if len(rest) > 2 else ""
            if method == "PATCH" and not action:
                fields = set(body)
                free = {"approved", "locked"}
                if fields - free:
                    need_unlocked(s)
                for f in ("beat", "image_prompt", "video_prompt"):
                    if f in body and body[f] != s[f]:
                        s[f] = body[f]
                        s["edited"][f] = bool(body.get("hand", True))
                for f in ("extra", "static", "locked"):
                    if f in body:
                        s[f] = body[f]
                if body.get("ask"):
                    s.setdefault("asks", []).append({"text": body["ask"].strip()[:500], "at": time.time()})
                if "approved" in body:
                    if body["approved"] and s["still"] is None:
                        raise HttpError(400, "Generate a still before approving.")
                    s["approved"] = bool(body["approved"])
                if "ref_off" in body:
                    rid, off = body["ref_off"]["id"], body["ref_off"]["off"]
                    s["refs"]["off"] = [x for x in s["refs"]["off"] if x != rid] + ([rid] if off else [])
                if "still" in body:
                    pipeline.select_still(s, int(body["still"]))
                if "clip" in body:
                    s["clip"] = int(body["clip"]) if body["clip"] is not None else None
                song.save_plan()
                return state(song)
            if method == "DELETE" and not action:
                need_unlocked(s)
                # Hand the time to the next section (or the previous one) when it
                # touches and isn't locked, so deleting doesn't leave a hole.
                prev, nxt = song.neighbours(s)
                if nxt and not nxt["locked"] and abs(nxt["start"] - s["end"]) < 0.02:
                    nxt["start"] = s["start"]
                elif prev and not prev["locked"] and abs(prev["end"] - s["start"]) < 0.02:
                    prev["end"] = s["end"]
                song.plan.remove(s)
                song.save_plan()
                return state(song)
            if method == "DELETE" and action == "refs":
                need_unlocked(s)
                s["refs"]["add"] = [r for r in s["refs"]["add"] if r["id"] != rest[3]]
                song.save_plan()
                return state(song)
            if method == "POST" and action == "mark-failed":
                pipeline.mark_job_failed(song, s["uid"])
                return state(song)
            if method == "POST" and action == "retry":
                s["failed"] = None
                song.save_plan()
                return state(song)
        if method == "POST" and action == "propose":
            need_unlocked(s)
            return pipeline.propose(song, s["uid"], bool(body.get("write_beat")))

    raise HttpError(404, f"No route {method} {path}")


# ================================================================== server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/api/" in self.path and not self.path.endswith(("/peaks",)) and self.command != "GET":
            sys.stderr.write("%s %s\n" % (self.command, self.path))

    def _send(self, code, payload, ctype="application/json", extra=None):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _file(self, path):
        if not path.is_file():
            return self._send(404, {"error": "Not found"})
        size = path.stat().st_size
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        start, end = 0, size - 1
        if rng and (rng.group(1) or rng.group(2)):
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else size - 1
            else:
                start = max(0, size - int(rng.group(2)))
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "max-age=3600" if "/stills/" in str(path) or "/raw/" in str(path) else "no-cache")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _handle(self):
        url = urlparse(self.path)
        path = url.path
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if not path.startswith("/api/"):
            target = STATIC / ("index.html" if path in ("/", "") or path.startswith("/song/") else path.lstrip("/"))
            target = target.resolve()
            if STATIC.resolve() not in target.parents and target != STATIC.resolve() / "index.html":
                return self._send(404, {"error": "Not found"})
            return self._file(target)
        m = re.match(r"^/api/songs/([a-z0-9-]+)/file/(.+)$", path)
        if m and self.command in ("GET", "HEAD"):
            try:
                song = songs.get(m.group(1))
            except KeyError:
                return self._send(404, {"error": "No song"})
            target = (song.dir / m.group(2)).resolve()
            if song.dir.resolve() not in target.parents:
                return self._send(404, {"error": "Not found"})
            return self._file(target)

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = raw
        if "json" in (self.headers.get("Content-Type") or "") or (raw[:1] in (b"{", b"[")):
            body = json.loads(raw or b"{}")
        elif not raw:
            body = {}
        try:
            self._send(200, route(self.command, path, q, body, self))
        except HttpError as e:
            self._send(e.code, {"error": str(e)})
        except providers.ProviderError as e:
            print(f"  provider error: {e}", file=sys.stderr)
            self._send(502, {"error": str(e)})
        except (KeyError, ValueError) as e:
            print(f"  {type(e).__name__}: {e}", file=sys.stderr)
            self._send(400, {"error": str(e).strip("'\"")})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _handle


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # The browser drops idle keep-alive connections and cancels media
        # requests when you seek; that's normal, not worth a traceback.
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main():
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8780
    songs.SONGS.mkdir(exist_ok=True)
    for s in songs.library():
        pipeline.resume_jobs(songs.get(s["slug"]))
    server = Server(("127.0.0.1", port), Handler)
    print(f"Music video app on http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
