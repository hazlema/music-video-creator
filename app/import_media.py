"""Copy an mv/ song folder (like media/) into songs/ as an app project.

Usage: uv run --with imageio-ffmpeg --with numpy --with pillow python app/import_media.py media "Tiger song"

Copies the audio, lyrics, brief and transcript; re-aligns to get word
timings; keeps the old shot windows and prompts; brings in each still as
version 1 and each raw clip as clip 1 (approved). The source folder is only read.
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline  # noqa: E402
import songs  # noqa: E402
from songs import new_section, read_json  # noqa: E402


def main():
    src = Path(sys.argv[1]).resolve()
    song = songs.create(sys.argv[2] if len(sys.argv) > 2 else src.name)
    for f in ("song.wav", "lyrics.txt", "brief.txt", "transcript.json"):
        if (src / f).exists():
            shutil.copy2(src / f, song.dir / f)
    song.reset_length()
    song.meta["style"] = "Moody indie music video, muted teal and rust colour grade, rain, overcast light, soft film grain."
    song.save_meta()
    pipeline.align(song)

    old = read_json(src / "plan.json", [])
    shots = []
    for p in old:
        s = new_section(p["start"], p["end"], image_prompt=p["image_prompt"].replace(pipeline.NO_TEXT.strip(), "").strip(),
                        video_prompt=p["video_prompt"].replace("No text, captions or words on screen.", "").strip())
        still = src / "stills" / f"{p['id']}.png"
        if still.exists():
            name = f"{s['uid']}-v1.png"
            shutil.copy2(still, song.dir / "stills" / name)
            s["stills"].append({"n": 1, "file": name, "prompt": s["image_prompt"], "model": "OpenArt (imported)",
                                "cost": 0, "at": still.stat().st_mtime})
            s["still"] = 1
            s["approved"] = True
        raw = src / "raw" / f"{p['id']}.mp4"
        if raw.exists():
            name = f"{s['uid']}-c1.mp4"
            shutil.copy2(raw, song.dir / "raw" / name)
            s["clips"].append({"n": 1, "file": name, "still": 1, "seconds": p["duration"],
                               "model": "OpenArt Grok (imported)", "cost": 0, "at": raw.stat().st_mtime})
            s["clip"] = 1
        shots.append(s)

    plan, cursor = [], 0.0
    for s in shots:
        if s["start"] - cursor >= pipeline.MIN_GAP:
            plan.append(new_section(cursor, s["start"], "gap"))
        plan.append(s)
        cursor = s["end"]
    if song.length - cursor >= pipeline.MIN_GAP:
        plan.append(new_section(cursor, song.length, "gap"))
    gap_img = src / "gap_stage.png"
    if gap_img.exists():
        for s in plan:
            if s["kind"] == "gap":
                name = f"{s['uid']}-v1.png"
                shutil.copy2(gap_img, song.dir / "stills" / name)
                s["stills"].append({"n": 1, "file": name, "prompt": "Imported gap image", "model": "imported",
                                    "cost": 0, "at": gap_img.stat().st_mtime})
                s["still"] = 1
    song.plan = plan
    song.save_plan()
    print(f"Imported {src} as songs/{song.slug}: {len(plan)} sections, "
          f"{sum(1 for s in plan if s['still'])} stills, {sum(1 for s in plan if s['clip'])} clips")


if __name__ == "__main__":
    main()
