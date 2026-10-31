"""Record finished video URLs: python3 mv/setvideo.py <song_dir> <shot id>=<url> ..."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import read_json, write_json  # noqa: E402

song_dir = Path(sys.argv[1])
urls = dict(a.split("=", 1) for a in sys.argv[2:])
plan = read_json(song_dir / "plan.json")
for p in plan:
    if p["id"] in urls:
        p["video_url"] = urls[p["id"]]
write_json(song_dir / "plan.json", plan)
print(f"{sum(1 for p in plan if p['video_url'])}/{len(plan)} videos recorded")
