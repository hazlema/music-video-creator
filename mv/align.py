"""Step 2: time each line of lyrics.txt using the transcript.

Usage: python3 mv/align.py <song_dir>
Reads transcript.json + lyrics.txt, writes lines.json.

The transcript only has phrase-level times and mishears words, so:
  1. Each transcript word gets a time by spreading its phrase's span over its
     characters.
  2. Lyric words are aligned to transcript words (Needleman-Wunsch, fuzzy word
     match), skipping ad-libs and mishears.
  3. Matched lyric words take the transcript word's time; unmatched ones are
     interpolated from their neighbours. A line runs from its first word's start
     to its last word's end.
"""
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import fmt, read_json, write_json  # noqa: E402


def norm(w):
    return re.sub(r"[^a-z0-9]", "", w.lower().replace("’", "'"))


def transcript_words(segments):
    out = []
    for s in segments:
        words = [w for w in s["text"].split() if norm(w)]
        if not words:
            continue
        total = sum(len(w) + 1 for w in words)
        span = s["end"] - s["start"]
        pos = 0
        for w in words:
            a = s["start"] + span * pos / total
            pos += len(w) + 1
            out.append({"w": norm(w), "start": a, "end": s["start"] + span * pos / total})
    return out


def similar(a, b):
    return a == b or SequenceMatcher(None, a, b).ratio() >= 0.6


def align(lw, tw):
    """Return {lyric_word_index: transcript_word_index} for matched pairs."""
    n, m = len(lw), len(tw)
    MATCH, GAP = 2, -1
    score = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        score[i][0] = i * GAP
    for j in range(1, m + 1):
        score[0][j] = j * GAP
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            diag = score[i - 1][j - 1] + (MATCH if similar(lw[i - 1], tw[j - 1]["w"]) else -2)
            score[i][j] = max(diag, score[i - 1][j] + GAP, score[i][j - 1] + GAP)
    # Traceback runs from the end, so skipping transcript words first makes
    # repeated phrases (ad-libs) match their earliest occurrence.
    pairs, i, j = {}, n, m
    while i > 0 and j > 0:
        is_match = similar(lw[i - 1], tw[j - 1]["w"])
        if score[i][j] == score[i][j - 1] + GAP:
            j -= 1
        elif is_match and score[i][j] == score[i - 1][j - 1] + MATCH:
            pairs[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif score[i][j] == score[i - 1][j - 1] - 2:
            i, j = i - 1, j - 1
        elif score[i][j] == score[i - 1][j] + GAP:
            i -= 1
        else:
            j -= 1
    return pairs


def interpolate(times, known, word_dur):
    """Fill None entries in times from the nearest known neighbours by index.
    Past the first/last known word, extrapolate at word_dur seconds per word."""
    idx = sorted(known)
    for k in range(len(times)):
        if times[k] is not None:
            continue
        before = [i for i in idx if i < k]
        after = [i for i in idx if i > k]
        if before and after:
            a, b = before[-1], after[0]
            times[k] = times[a] + (times[b] - times[a]) * (k - a) / (b - a)
        elif before:
            times[k] = times[before[-1]] + word_dur * (k - before[-1])
        elif after:
            times[k] = max(0.0, times[after[0]] - word_dur * (after[0] - k))
    return times


def main():
    song_dir = Path(sys.argv[1])
    segments = read_json(song_dir / "transcript.json")["segments"]
    lyric_lines = [l.strip() for l in (song_dir / "lyrics.txt").read_text().splitlines() if l.strip()]

    tw = transcript_words(segments)
    lw, owner = [], []
    for li, line in enumerate(lyric_lines):
        for w in line.split():
            if norm(w):
                lw.append(norm(w))
                owner.append(li)

    pairs = align(lw, tw)
    starts = [tw[pairs[k]]["start"] if k in pairs else None for k in range(len(lw))]
    ends = [tw[pairs[k]]["end"] if k in pairs else None for k in range(len(lw))]
    word_dur = sorted(w["end"] - w["start"] for w in tw)[len(tw) // 2]
    interpolate(starts, pairs.keys(), word_dur)
    interpolate(ends, pairs.keys(), word_dur)

    lines, prev_end = [], 0.0
    for li, text in enumerate(lyric_lines):
        ks = [k for k in range(len(lw)) if owner[k] == li]
        matched = sum(1 for k in ks if k in pairs)
        start = max(starts[ks[0]], prev_end)
        end = max(ends[ks[-1]], start + 0.5)
        prev_end = end
        lines.append({
            "i": li,
            "text": text,
            "start": round(start, 2),
            "end": round(end, 2),
            "matched": f"{matched}/{len(ks)}",
        })

    write_json(song_dir / "lines.json", lines)
    for l in lines:
        flag = "" if l["matched"].split("/")[0] != "0" else "   <- guessed (no words heard)"
        print(f"{l['i']:2d} [{fmt(l['start'])} -> {fmt(l['end'])}] ({l['matched']:>5}) {l['text']}{flag}")


if __name__ == "__main__":
    main()
