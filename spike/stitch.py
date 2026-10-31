"""Throwaway spike: stitch per-lyric clips into one video with the song underneath.

Usage:
    uv run --with imageio-ffmpeg python spike/stitch.py spike/test/manifest.json

manifest.json:
    {"audio": "media/song.wav", "output": "spike/test/test.mp4",
     "clips": [{"file": "spike/test/vid/00.mp4", "start": 15.26, "end": 22.02}, ...]}

Each clip is trimmed (or its last frame held) to exactly end - start seconds,
so the cuts land on the song's timestamps. Clips' own audio is dropped.
"""
import json
import subprocess
import sys

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
W, H, FPS = 736, 400, 24


def main():
    m = json.load(open(sys.argv[1]))
    clips = m["clips"]
    t0, t1 = clips[0]["start"], clips[-1]["end"]

    args = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
    for c in clips:
        args += ["-i", c["file"]]
    args += ["-ss", str(t0), "-t", str(t1 - t0), "-i", m["audio"]]

    filters = []
    for i, c in enumerate(clips):
        d = c["end"] - c["start"]
        filters.append(
            f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},"
            f"tpad=stop_mode=clone:stop_duration={d},trim=duration={d},setpts=PTS-STARTPTS[v{i}]"
        )
    filters.append("".join(f"[v{i}]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[v]")

    args += [
        "-filter_complex", ";".join(filters),
        "-map", "[v]", "-map", f"{len(clips)}:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-shortest",
        m["output"],
    ]
    subprocess.run(args, check=True)
    print(f"Wrote {m['output']} ({t1 - t0:.2f}s, song {t0:.2f}s -> {t1:.2f}s)")


if __name__ == "__main__":
    main()
