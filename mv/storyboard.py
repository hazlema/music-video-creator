"""Step 3: plan one shot per lyric line with an LLM, using the song brief.

Usage: python3 mv/storyboard.py <song_dir> [--model anthropic/claude-sonnet-5.5]
Reads brief.txt + lines.json, writes plan.json.

The whole song goes in one request so the shots form a consistent story.
Each plan entry gets an image prompt (for the still) and a motion prompt (for
Grok), both text-free; captions are added later by finish.py.
"""
import json
import math
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import api_key, fmt, read_json, timecode, write_json  # noqa: E402

URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"

# A clip runs from its line's start to the next line's start when the gap is
# small (so cuts land on the next line), otherwise it ends shortly after the
# line and the gap is left for the editor.
MAX_BRIDGE_GAP = 2.0
TAIL = 0.75
GROK_MAX = 15

SYSTEM = """You are a music video director writing a shot list.
You get a song brief and the song's lyric lines with timings. Plan one shot per line.
The shots are generated independently by an image model and then animated by a video model, so:
- Every image prompt must be fully self-contained: restate the setting, characters' appearance and the visual style each time. Never say "same as before".
- Keep recurring characters described identically every time they appear.
- Interpret lyrics through the brief (e.g. "purring" refers to the tiger, not fire).
- Never put text, words, captions, signage lettering or logos in any shot.
- Keep it tense and dreamlike, never graphic or gory.
Reply with JSON only, no prose:
{"lines": [{"i": <line index>, "scene": "<detailed image prompt for the still, 60-120 words>", "motion": "<what moves and how the camera moves during the clip, 20-50 words>"}]}"""


def clip_window(lines, k):
    line = lines[k]
    start = line["start"]
    nxt = lines[k + 1]["start"] if k + 1 < len(lines) else None
    if nxt is not None and nxt - line["end"] <= MAX_BRIDGE_GAP:
        end = nxt
    else:
        end = line["end"] + TAIL
    end = min(end, start + GROK_MAX)
    return round(start, 2), round(end, 2)


def ask(model, brief, lines):
    lyric_table = "\n".join(f'{l["i"]}. [{fmt(l["start"])}-{fmt(l["end"])}] {l["text"]}' for l in lines)
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"SONG BRIEF:\n{brief}\n\nLYRICS:\n{lyric_table}"},
        ],
        "max_tokens": 16000,
    }).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()}")
    text = data["choices"][0]["message"]["content"]
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    usage = data.get("usage", {})
    print(f"storyboard: {model}, cost {usage.get('cost')}", file=sys.stderr)
    return {s["i"]: s for s in json.loads(text)["lines"]}


def main():
    song_dir = Path(sys.argv[1])
    model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else DEFAULT_MODEL
    brief = (song_dir / "brief.txt").read_text().strip()
    lines = read_json(song_dir / "lines.json")

    shots = ask(model, brief, lines)
    plan = []
    for k, line in enumerate(lines):
        start, end = clip_window(lines, k)
        shot = shots.get(line["i"])
        if not shot:
            sys.exit(f"Storyboard is missing line {line['i']}: {line['text']}")
        plan.append({
            "id": timecode(start),
            "i": line["i"],
            "text": line["text"],
            "start": start,
            "end": end,
            "duration": max(1, math.ceil(end - start)),
            "image_prompt": shot["scene"] + " No text, no words, no lettering anywhere in the image.",
            "video_prompt": shot["motion"] + " No text, captions or words on screen.",
            "still_url": None,
            "video_url": None,
        })

    write_json(song_dir / "plan.json", plan)
    for p in plan:
        print(f'{p["id"]}  {p["duration"]:2d}s  {p["text"]}\n          {p["image_prompt"][:110]}...')
    print(f"\n{len(plan)} shots, {sum(p['duration'] for p in plan)}s of video total", file=sys.stderr)


if __name__ == "__main__":
    main()
