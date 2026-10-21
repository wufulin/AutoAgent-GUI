"""Evaluate exported execution summaries against independent human labels."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from AutoGLM_GUI.evaluation import evaluate_records, read_json_records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "runs", type=Path, help="JSONL summaries or exported task events"
    )
    parser.add_argument("--labels", type=Path, help="JSONL human outcome labels")
    parser.add_argument("--group-by", choices=("route", "variant"), default="route")
    parser.add_argument("--baseline", help="Baseline variant; requires --candidate")
    parser.add_argument("--candidate", help="Candidate variant; requires --baseline")
    parser.add_argument(
        "--output", type=Path, help="Write report JSON (default: stdout)"
    )
    args = parser.parse_args()
    try:
        report = evaluate_records(
            read_json_records(args.runs),
            read_json_records(args.labels) if args.labels else (),
            group_by=args.group_by,
            baseline=args.baseline,
            candidate=args.candidate,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
