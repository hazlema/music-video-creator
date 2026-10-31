# Idea (parked 2026-10-29): song → music video web app

Turn the `mv/` pipeline into a video production workflow behind a page.
Parked on purpose; come back to it after current projects.

## Where it runs
Local web app: small Python server reusing `mv/` + one HTML page.
Keys stay in `.env`; each song is a folder laid out like `media/`, so the UI
and the CLI scripts work on the same files. (A browser-only artifact can't do
ffmpeg captions or the Blender file.)

## Stages
1. **Setup** - upload song.wav, paste lyrics, write the brief, optional character reference image.
2. **Transcribe** - nemotron-3.5-transcribe for *timing only*; align to the real lyrics (fixes mishears like "burning"/"Purring"). Nudge timings on the timeline.
3. **Storyboard** - one LLM call writes scene + motion prompts from the brief. Editable, nearly free. This is what keeps the images consistent.
4. **Stills (main screen = timeline)**
   - waveform on top, one block per line sized to its duration, thumbnails
   - click a block: see old prompt, edit it, add per-shot extra instructions, toggle character ref, regenerate
   - every regen is a new version (flip back if worse)
   - drag across an empty stretch to make a gap block (e.g. guitarist on stage), optionally "static"
   - draft/approved status per block
5. **Animation** - cost estimate first (~36 credits/sec was most of the spend); send approved blocks to Grok with still + motion prompt + ceil(length) ≤ 15s; ~4 at a time; re-roll any clip.
6. **Export** - burn captions, .srt, `music_video.blend` (Video Editing workspace), preview MP4 in page.

## Data
Existing `plan.json` per song, plus: `kind` (lyric|gap), `extra`, `versions[]`, `approved`.

## Open risk - check first
OpenArt is only reachable through Claude's connector, so the app needs direct APIs:
xAI Grok Imagine for image→video. Spend a few dollars confirming quality before building.

Stills: plan is a **local Flux model**, probably FLUX.2 [klein] (small and fast,
supports reference images). That makes stills and per-shot regens free, so
iterating on the timeline costs nothing; only video still costs money. Check first:
does it keep the narrator consistent from a reference image as well as Nano Banana 2
did, and is it fast enough on local VRAM for regen-on-click.
Fallback is fine either way: Nano Banana via API is only ~$0.014/image (per Frosty),
so stills are cheap even when they're not local. Pick on consistency, not price.

## History
Tried a version of this a few months earlier; the models weren't good enough yet.
The tiger song (media/) was the first run where it worked - "90% good to go".
