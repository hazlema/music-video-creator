"""Step 1: timed words from elevenlabs/scribe-v2 via OpenRouter.

Usage: python3 mv/transcribe.py <song_dir> [--full] [--model <id>]
Reads <song_dir>/song.wav, writes <song_dir>/transcript.json.
--full sends the original WAV instead of a 16 kHz mono downsample.
--model picks another OpenRouter transcription model.

When the model returns per-word times, each sung word becomes its own
segment, so align.py gets exact timing. (Since late October 2026 nemotron
returns one segment for the whole song, which carries no timing at all.)
"""
import base64
import io
import json
import sys
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import api_key, fmt, write_json  # noqa: E402

MODEL = "elevenlabs/scribe-v2"
MAX_WORD = 1.5   # a held last note can stretch one "word" over the whole outro
URL = "https://openrouter.ai/api/v1/audio/transcriptions"


def downsample_16k_mono(path):
    with wave.open(str(path)) as w:
        ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    assert width == 2, "expects 16-bit PCM"
    x = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch).mean(axis=1)
    # Crude resample via linear interpolation; fine for speech recognition.
    target = 16000
    t_new = np.arange(0, len(x) / rate, 1 / target)
    y = np.interp(t_new, np.arange(len(x)) / rate, x).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as o:
        o.setnchannels(1)
        o.setsampwidth(2)
        o.setframerate(target)
        o.writeframes(y.tobytes())
    return buf.getvalue()


def word_segments(data):
    """One segment per sung word from a response with per-word times, or None.
    Skips audio events like "[upbeat country music]" and music symbols."""
    words = [w for w in data.get("words") or []
             if w.get("type", "word") == "word" and "start" in w and "end" in w
             and (w.get("word") or "").strip().strip("♪♫ ")
             and not (w["word"].strip().startswith(("[", "(")) and w["word"].strip().endswith(("]", ")")))]
    if not words:
        return None
    return [{"start": round(w["start"], 3), "end": round(min(w["end"], w["start"] + MAX_WORD), 3),
             "text": w["word"].strip()} for w in words]


def main():
    song_dir = Path(sys.argv[1])
    model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else MODEL
    key = api_key()
    wav = song_dir / "song.wav"
    audio = wav.read_bytes() if "--full" in sys.argv else downsample_16k_mono(wav)
    print(f"Sending {len(audio) / 1e6:.1f} MB of audio to {model}...", file=sys.stderr)

    body = json.dumps({
        "model": model,
        "input_audio": {"data": base64.b64encode(audio).decode(), "format": "wav"},
        "response_format": "verbose_json",
        "timestamp_granularities": ["word", "segment"],
    }).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()}")

    segs = word_segments(data)
    if segs:
        data["phrases"], data["segments"] = data.get("segments"), segs
    elif any(s["end"] - s["start"] > 20 for s in data.get("segments") or []):
        print("Warning: no word timing came back, so align.py can't place the lyrics. Try --model elevenlabs/scribe-v2.",
              file=sys.stderr)
    write_json(song_dir / "transcript.json", data)
    for s in data.get("segments") or []:
        print(f"[{fmt(s['start'])} -> {fmt(s['end'])}] {s['text'].strip()}")
    if "usage" in data:
        print(f"\nusage: {data['usage']}", file=sys.stderr)


if __name__ == "__main__":
    main()
