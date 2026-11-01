<img src="assets/icon.svg" width="64" height="64" alt="">

# music-video-creator

Non-linear music video editor with export to MP4 and Blender files.

![The timeline: a still for every shot under the song's waveform, captions on top, the inspector on the right](assets/title.png)

Give it a song and its lyrics. It times every lyric line to the audio, writes a
storyboard, makes a still for each shot, and animates the stills you approve.
You edit on a timeline, then export one MP4 with burned-in captions, plus a
Blender project with every clip at its timecode.

Models run through [OpenRouter](https://openrouter.ai): transcription
(ElevenLabs Scribe v2, which times every word), the storyboard, stills and
video. Stills can also run free on your own GPU with FLUX.2 Klein (see
[Local stills](#local-stills-optional-free)). A whole song in API stills costs
about $1. Clips are priced per second: new songs default to Grok Imagine Video
1.5 Lite at $0.02/s (480p), about $4 for a 40-shot song; full Grok 1.5 is
$0.08/s, and any OpenRouter video model can be picked per song.

## Setup

You need [uv](https://docs.astral.sh/uv/) and an OpenRouter key in `.env` at the
repo root:

```
OPENROUTER_API_KEY=sk-or-...
```

Blender is optional. If `/opt/blender-5.1.2-linux-x64/blender` exists, export
also writes `music_video.blend`; to use another install, change `BLENDER` in
`app/pipeline.py`.

## The web app

```
uv run --with imageio-ffmpeg --with numpy --with pillow python app/server.py
```

Then open http://localhost:8780. Run it in your own terminal and leave it
running; clip jobs that are in flight when it stops resume when it starts again.

| Switch | What it does |
|---|---|
| `--port <n>` | Listen on port `n` instead of 8780. The server only accepts connections from this machine. |

Songs are saved in `songs/<name>/` (not in git): audio, lyrics, plan, every
still and clip version, and exports.

### Keyboard and mouse (timeline)

| Input | What it does |
|---|---|
| Space | Play or pause |
| ← / → | Step to the previous or next section |
| Click / Shift-click | Select a section / select a range |
| Alt+A | Select every section |
| Esc | Clear the selection |
| + / − or the mouse wheel | Zoom, keeping the playhead in place |
| Shift+wheel | Scroll along the song |
| Hold the middle button and drag | Pan the timeline |
| Ctrl+Enter | Send the request in **Ask for a change** |

Section edges can only be dragged with **Lock timing** unticked in the timeline
toolbar; it's on by default so a stray drag can't move a cut.

### Import an mv/ song folder

```
uv run --with imageio-ffmpeg --with numpy --with pillow python app/import_media.py <folder> ["Title"]
```

| Argument | What it does |
|---|---|
| `<folder>` | An mv/ song folder (like `media/`). It is only read, never changed. |
| `"Title"` | The song's name in the library. Defaults to the folder name. |

Brings in the audio, lyrics, brief and transcript, re-times the lyrics, keeps the
shots and prompts, and adds each still as version 1 and each clip as clip 1.

## Local stills (optional, free)

Stills can be made on your own GPU with
[FLUX.2 Klein 4B](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B)
(Apache 2.0) instead of a paid API: about 5 seconds and $0 a still. The model
runs in a separate process, the sidecar, so you start two things, each in its
own terminal:

```
# terminal 1: the local image model (port 8790)
cd sidecar
uv run python klein_server.py

# terminal 2: the app (port 8780)
uv run --with imageio-ffmpeg --with numpy --with pillow python app/server.py
```

The sidecar's first run installs PyTorch and diffusers into `sidecar/.venv` and
downloads the model (about 15 GB) into the Hugging Face cache. It's ready when it
prints `Klein sidecar on http://127.0.0.1:8790 (fast mode)`.

Then pick **FLUX.2 Klein 4B (local, your GPU)** as the Stills model in a song's
Setup; the hint under it says whether the sidecar is running. If it isn't, the
section shows the command to start it instead of failing quietly.

Klein handles most shots. When one is beyond it (a busy scene, several
characters), select the section and use **Regenerate with…** to make just that
still with an API model such as GPT Image or Nano Banana; the song keeps Klein,
and the new still is added as another version. It works on a selection too.

| Switch | What it does |
|---|---|
| `--port <n>` | Listen on port `n` instead of 8790. Point the app at it with `MV_SIDECAR=http://127.0.0.1:<n>`. |
| `--offload` | Keep the model in system RAM and move each part onto the GPU only while it runs. Slower (about 25 s a still instead of 5 s) but needs only about 8 GB of VRAM. Without it, the sidecar switches to this mode by itself if the GPU runs out of memory. |

Needs an NVIDIA GPU: 16 GB runs the fast mode with room to spare for other
programs holding a few GB. Reference images work, so characters stay
consistent from shot to shot.

## Command-line pipeline (mv/)

The scripts the app grew out of. Each one works on a song folder holding
`song.wav`, `lyrics.txt` and `brief.txt`, and the steps run in order.

| Step | Command | What it does |
|---|---|---|
| 1 | `python3 mv/transcribe.py <song_dir> [--full] [--model <id>]` | Timed words from the audio, written to `transcript.json`. Sends a 16 kHz mono copy; `--full` sends the original WAV. `--model` picks the OpenRouter transcription model (default `elevenlabs/scribe-v2`, which times every word). |
| 2 | `python3 mv/align.py <song_dir>` | Times each line of `lyrics.txt` against the transcript, written to `lines.json`. |
| 3 | `python3 mv/storyboard.py <song_dir> [--model <id>]` | One shot per lyric line with an image prompt and a motion prompt, written to `plan.json`. `--model` picks the OpenRouter model (default `anthropic/claude-sonnet-5.5`). |
| 4 | `python3 mv/finish.py <song_dir> fetch` | Downloads each shot's `still_url` / `video_url` from `plan.json`. |
| 4 | `python3 mv/finish.py <song_dir> sheet` | Makes `stills/contact_sheet.jpg` so you can review every still at once. |
| 5 | `uv run --with imageio-ffmpeg python mv/finish.py <song_dir> clips` | Trims each clip to its slot in the song: `clips/` with captions, `clean/` without, plus `lyrics.srt`. |
| 6 | `uv run --with imageio-ffmpeg python mv/preview.py <song_dir> [gap_image]` | `preview.mp4`: every clip at its exact time over a background image, with the song. The image defaults to `gap_stage.png`. |
| 7 | `blender -b --factory-startup --python mv/blend.py -- <song_dir> [gap_image]` | `music_video.blend`: song, background image, captioned and clean clips on separate channels, a marker per lyric line. |

Helpers for recording finished media URLs in `plan.json`:

| Command | What it does |
|---|---|
| `python3 mv/setvideo.py <song_dir> <shot id>=<url> ...` | Sets `video_url` for the given shots. |
| `python3 mv/record.py <song_dir> still\|video <jobs.txt> <creation_list.json>...` | Copies finished OpenArt URLs into `plan.json` and lists shots that are still pending or failed. |

More about how the app is put together: [app/README.md](app/README.md).

MIT licensed, see [LICENSE](LICENSE). Created in [T3 Code](https://t3.codes).
