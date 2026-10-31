"""Copy finished OpenArt URLs into plan.json.

Usage: python3 mv/record.py <song_dir> still|video <jobs.txt> <creation_list.json>...

jobs.txt has one "<shot id> <historyId>" per line (written while submitting).
creation_list.json files are saved openart_creation_list responses.
Prints which shots are still pending or failed.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import read_json, write_json  # noqa: E402


def main():
    song_dir, kind, jobs_file, *dumps = sys.argv[1:]
    field = f"{kind}_url"
    jobs = dict(l.split() for l in Path(jobs_file).read_text().splitlines() if l.strip())

    by_history = {}
    for f in dumps:
        for item in json.loads(Path(f).read_text())["items"]:
            by_history[item["historyId"]] = item

    plan = read_json(song_dir_path := Path(song_dir) / "plan.json")
    waiting = []
    for p in plan:
        h = jobs.get(p["id"])
        item = by_history.get(h)
        if item and item["status"] == "completed" and item["url"]:
            p[field] = item["url"]
            p[f"{kind}_id"] = item["id"]
        elif not p[field]:
            waiting.append(f"{p['id']} ({item['status'] if item else 'not listed' if h else 'no job'})")
    write_json(song_dir_path, plan)
    done = sum(1 for p in plan if p[field])
    print(f"{done}/{len(plan)} {kind}s recorded")
    if waiting:
        print("waiting:", ", ".join(waiting))


if __name__ == "__main__":
    main()
