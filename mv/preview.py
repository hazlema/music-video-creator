"""Full-song preview: every clip placed at its timestamp over a gap image, with the song.

Usage: uv run --with imageio-ffmpeg python mv/preview.py <song_dir> [gap_image]
Writes <song_dir>/preview.mp4. Clips are overlaid at their exact start times
(not concatenated), so frame rounding can't drift the sync.
"""
import subprocess
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import ffmpeg, read_json  # noqa: E402

W, H, FPS = 736, 400, 24


def main():
    song_dir = Path(sys.argv[1])
    gap = Path(sys.argv[2]) if len(sys.argv) > 2 else song_dir / "gap_stage.png"
    plan = [p for p in read_json(song_dir / "plan.json") if (song_dir / "clips" / f"{p['id']}.mp4").exists()]
    with wave.open(str(song_dir / "song.wav")) as w:
        length = w.getnframes() / w.getframerate()

    args = [ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-framerate", str(FPS), "-t", f"{length:.3f}", "-i", str(gap)]
    for p in plan:
        args += ["-i", str(song_dir / "clips" / f"{p['id']}.mp4")]
    args += ["-i", str(song_dir / "song.wav")]

    chain = [f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[b0]"]
    for n, p in enumerate(plan, 1):
        chain.append(f"[{n}:v]setpts=PTS+{p['start']}/TB[c{n}]")
        chain.append(f"[b{n - 1}][c{n}]overlay=eof_action=pass[b{n}]")
    args += ["-filter_complex", ";".join(chain), "-map", f"[b{len(plan)}]", "-map", f"{len(plan) + 1}:a",
             "-r", str(FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
             "-c:a", "aac", "-b:a", "192k", "-shortest", str(song_dir / "preview.mp4")]
    subprocess.run(args, check=True)
    print(f"wrote {song_dir / 'preview.mp4'} ({length:.1f}s, {len(plan)} clips)")


if __name__ == "__main__":
    main()
