"""Rebuild fills/equity from saved exam plans without calling any AI provider."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exam_replay.runner import ExamReplay, write_report  # noqa: E402
from exam_v2.snapshot import ExamMode  # noqa: E402
import yaml  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    source = Path(args.source)
    if not source.is_absolute():
        source = ROOT / source
    data = json.loads(source.read_text(encoding="utf-8"))
    days = data["days"]
    symbols = yaml.safe_load((ROOT / "config/watchlist.yaml").read_text(encoding="utf-8"))["symbols"]
    replay = ExamReplay(
        symbols=symbols, mode=ExamMode(data["exam_mode"]), model=data.get("model", "deepseek-flash"),
        transport=None, short_locates=True, max_focus=4,
    )
    result = replay.run_saved(days, data["decisions"])
    report = replay.report(result)
    report["days"] = days
    report["calls"] = data.get("calls", [])
    report["warnings"] = data.get("warnings", [])
    report["no_real_orders"] = True
    report["no_production_memory"] = True
    out = Path(args.out) if args.out else source
    if not out.is_absolute():
        out = ROOT / out
    write_report(report, out)
    print(json.dumps({"out": str(out), "metrics": report["metrics"],
                      "fills": len(report["fills"]), "trades": len(report["trades"]),
                      "open_positions": report["open_positions"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
