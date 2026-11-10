"""Record real runs of the demo's example questions, for the web page to replay.

The page replays these when the live API is unreachable or a visitor hits the rate limit,
so the examples always show a genuine run.

    python scripts/record_demo.py                 # writes web/recorded.json
    python scripts/record_demo.py --only cancel   # re-record one example
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.sql_analyst import build_sql_analyst  # noqa: E402
from api.server import stream_answer              # noqa: E402
from utils.llm_pick import pick_llm               # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()

    examples = json.loads((ROOT / "web" / "examples.json").read_text(encoding="utf-8"))
    out_path = ROOT / "web" / "recorded.json"
    old = {e["id"]: e for e in json.loads(out_path.read_text(encoding="utf-8"))["examples"]} if out_path.exists() else {}
    graph = build_sql_analyst()

    recorded = []
    for ex in examples:
        if args.only and ex["id"] not in args.only:
            recorded.append(old.get(ex["id"], ex))
            continue
        events = [json.loads(chunk[6:]) for chunk in stream_answer(ex["question"], pick_llm, graph)]
        final = events[-1] if events else {}
        print(f"{ex['id']:<8} {final.get('status', '?'):<9} attempts={final.get('attempts', 0)}  {final.get('ms', 0) / 1000:.1f}s")
        recorded.append({**ex, "simulated": False, "events": events})

    out_path.write_text(json.dumps({
        "note": f"Real runs recorded {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC with scripts/record_demo.py",
        "examples": recorded}, indent=1), encoding="utf-8")
    print(f"wrote {out_path.relative_to(ROOT)}")
