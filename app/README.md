# Song → music video (v1 prototype)

```
uv run --with imageio-ffmpeg --with numpy --with pillow python app/server.py
```
Open http://localhost:8780. Uses `OPENROUTER_API_KEY` from `.env` for everything:
transcription, the storyboard LLM, stills and video (OpenRouter's `/videos` API).

Songs live in `songs/<slug>/`, laid out like `media/` (see `songs.py`). To bring
an mv/ folder in with its stills and clips as version 1:

```
uv run --with imageio-ffmpeg --with numpy --with pillow python app/import_media.py media "Tiger song"
```

## Files
- `server.py`: HTTP routes and the JSON state the page polls
- `pipeline.py`: transcribe/align (reuses `mv/align.py`), storyboard, stills, clip jobs, export (reuses `mv/blend.py`)
- `providers.py`: every OpenRouter call; stills sit behind `StillProvider` so a local Flux model can replace `OpenRouterStills`
- `songs.py`: the song folder and section model
- `static/`: the page (no build step)

## What v1 covers
Setup (audio, lyrics, brief, style line, global references, models) · transcribe and
match to real lyrics, low-confidence lines flagged, caption edges draggable ·
storyboard in one call with a visual rule and a beat per section; hand-edited fields
are kept · timeline with waveform cut into sections, zoom/fit, Shift-click ranges,
play stills/clips with the song · inspector: beat, prompts (Save never generates),
rewrite-from-beat preview with word diff, **Ask for a change** (type "the fence should be red" for one section or a selection; the storyboard model merges it into the prompts with the smallest edit, flags conflicts with the visual rule, folds in extra instructions, and you accept the diff; Polish wording is the opt-in freer rewrite; requests are kept per section), extra instructions, global refs with
per-section switches plus section-only refs, versions, approve, static, lock,
Failed + Retry · Split into shots overlay, Merge, drag boundaries (only with Lock timing unticked; it is on by default), drag an empty
stretch to make a gap · cost check before animating, approved non-static only, four
at a time, re-roll keeps versions · paid jobs written to `jobs.json` before sending;
a restart mid-send marks the job Unknown (never resent) until you mark it failed ·
export: captions burned in, every piece trimmed to its section, still fallback with
a slow push-in (listed first, with "Use the old clip anyway"), `lyrics.srt`,
`music_video.blend`, in-page player.

Not yet: undo/redo, Copy look, re-run storyboard for a selection, recurrence links,
needs-attention filter, local Flux.
