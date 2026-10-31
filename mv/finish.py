"""Steps 4-6: download generated media, review stills, build the final clips.

Usage:
    python3 mv/finish.py <song_dir> fetch    # download still_url/video_url from plan.json
    python3 mv/finish.py <song_dir> sheet    # stills/contact_sheet.jpg for review
    uv run --with imageio-ffmpeg python mv/finish.py <song_dir> clips

Outputs of `clips`:
    clips/<timecode>.mp4   captioned, trimmed to the clip's slot in the song
    clean/<timecode>.mp4   same without captions
    lyrics.srt             every line, timed to the song (for Blender)
Clips are silent; lay the song under them in the editor.
"""
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import read_json  # noqa: E402

W, H, FPS = 736, 400, 24
FONT = "DejaVu Sans"


def fetch(song_dir, plan):
    for kind, folder, ext in (("still_url", "stills", "png"), ("video_url", "raw", "mp4")):
        (song_dir / folder).mkdir(exist_ok=True)
        for p in plan:
            dest = song_dir / folder / f"{p['id']}.{ext}"
            if p[kind] and not dest.exists():
                # The CDN rejects urllib's default User-Agent.
                req = urllib.request.Request(p[kind], headers={"User-Agent": "curl/8"})
                with urllib.request.urlopen(req) as r:
                    dest.write_bytes(r.read())
                print(f"downloaded {dest}")


def sheet(song_dir, plan):
    from PIL import Image, ImageDraw, ImageFont

    tw, th, cols = 344, 192, 6
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
    rows = (len(plan) + cols - 1) // cols
    out = Image.new("RGB", (cols * tw, rows * (th + 36)), "black")
    draw = ImageDraw.Draw(out)
    for n, p in enumerate(plan):
        x, y = (n % cols) * tw, (n // cols) * (th + 36)
        f = song_dir / "stills" / f"{p['id']}.png"
        if f.exists():
            out.paste(Image.open(f).convert("RGB").resize((tw - 4, th)), (x + 2, y))
        draw.text((x + 4, y + th + 2), f"{p['id']}", fill="#9ad", font=font)
        draw.text((x + 4, y + th + 18), p["text"][:44], fill="white", font=font)
    dest = song_dir / "stills" / "contact_sheet.jpg"
    out.save(dest, quality=85)
    print(f"wrote {dest}")


def srt_time(t):
    ms = round(t * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def ass_time(t):
    cs = round(t * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_doc(text, duration):
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Lyric,{FONT},30,&H00FFFFFF,&H00000000,&H80000000,1,0,1,2,1,2,30,30,34

[Events]
Format: Layer, Start, End, Style, Text
Dialogue: 0,{ass_time(0)},{ass_time(duration)},Lyric,{{\\fad(250,200)}}{text}
"""


def render(src, dest, duration, ass=None):
    from common import ffmpeg

    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},"
          f"tpad=stop_mode=clone:stop_duration={duration},trim=duration={duration},setpts=PTS-STARTPTS")
    if ass:
        vf += f",subtitles=filename='{ass}'"
    subprocess.run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
                    "-vf", vf, "-an", "-r", str(FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                    str(dest)], check=True)


def clips(song_dir, plan):
    (song_dir / "clips").mkdir(exist_ok=True)
    (song_dir / "clean").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for p in plan:
            src = song_dir / "raw" / f"{p['id']}.mp4"
            if not src.exists():
                print(f"skip {p['id']}: no raw video yet")
                continue
            duration = round(p["end"] - p["start"], 3)
            ass = Path(tmp) / f"{p['id']}.ass"
            ass.write_text(ass_doc(p["text"], duration))
            render(src, song_dir / "clean" / f"{p['id']}.mp4", duration)
            render(src, song_dir / "clips" / f"{p['id']}.mp4", duration, ass)
            print(f"clip {p['id']}  {duration:5.2f}s  {p['text']}")

    srt = [f"{n}\n{srt_time(p['start'])} --> {srt_time(p['end'])}\n{p['text']}\n"
           for n, p in enumerate(plan, 1)]
    (song_dir / "lyrics.srt").write_text("\n".join(srt))
    print(f"wrote {song_dir / 'lyrics.srt'}")


def main():
    song_dir, cmd = Path(sys.argv[1]), sys.argv[2]
    plan = read_json(song_dir / "plan.json")
    {"fetch": fetch, "sheet": sheet, "clips": clips}[cmd](song_dir, plan)


if __name__ == "__main__":
    main()
