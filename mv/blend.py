"""Build a Blender VSE project with every clip at its song timecode.

Usage: /opt/blender-5.1.2-linux-x64/blender -b --factory-startup --python mv/blend.py -- <song_dir> [gap_image]
Writes <song_dir>/music_video.blend with paths relative to it:
    channel 1  song.wav
    channel 2  gap image, full length (shows through between clips)
    channel 3  captioned clips
    channel 4  clean clips (muted), to swap in if you restyle captions
    markers    one per lyric line
"""
import json
import sys
import wave
from pathlib import Path

import bpy

FPS = 24
W, H = 736, 400


def frame(t):
    return round(t * FPS) + 1


def add(kind, strips, **kw):
    """strips.new_* with fit_method when this Blender version supports it."""
    fn = getattr(strips, f"new_{kind}")
    try:
        return fn(fit_method="FILL", **kw)
    except TypeError:
        return fn(**kw)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    song_dir = Path(argv[0]).resolve()
    gap = Path(argv[1]).resolve() if len(argv) > 1 else song_dir / "gap_stage.png"
    plan = json.loads((song_dir / "plan.json").read_text())
    with wave.open(str(song_dir / "song.wav")) as w:
        length = w.getnframes() / w.getframerate()

    # Start from Blender's "Video Editing" template so the file opens in the
    # sequencer workspace rather than the 3D layout.
    bpy.ops.wm.read_homefile(app_template="Video_Editing")

    scene = bpy.context.scene
    scene.name = "Music Video"
    scene.render.fps = FPS
    scene.render.fps_base = 1
    scene.render.resolution_x, scene.render.resolution_y = W, H
    scene.render.resolution_percentage = 100
    scene.frame_start = 1
    scene.frame_end = frame(length)

    se = scene.sequence_editor_create()
    strips = se.strips if hasattr(se, "strips") else se.sequences

    strips.new_sound(name="song", filepath=str(song_dir / "song.wav"), channel=1, frame_start=1)
    bg = add("image", strips, name="gap", filepath=str(gap), channel=2, frame_start=1)
    bg.frame_final_duration = scene.frame_end

    for p in plan:
        start = frame(p["start"])
        dur = frame(p["end"]) - start
        for channel, folder, mute in ((3, "clips", False), (4, "clean", True)):
            f = song_dir / folder / f"{p['id']}.mp4"
            if not f.exists():
                continue
            s = add("movie", strips, name=f"{p['id']} {folder}", filepath=str(f), channel=channel, frame_start=start)
            s.frame_final_duration = dur
            s.mute = mute
        scene.timeline_markers.new(p["text"][:40], frame=start)

    out = song_dir / "music_video.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(out))
    bpy.ops.file.make_paths_relative()
    bpy.ops.wm.save_mainfile()
    print(f"wrote {out}: {len(plan)} clips, {scene.frame_end} frames")


main()
