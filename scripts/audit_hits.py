"""List every hit and every spurious blocking finding in a results file, for auditing by hand.

Usage: python scripts/audit_hits.py --cases cases/pydantic --results results/<run>/results.jsonl [--criteria criteria.json]

Keyword matching over-counts and under-counts (docs/METHODOLOGY.md). Reading
this list once per run is the check: does each hit describe the expected
defect, and is each spurious blocking finding really wrong?
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from review_bench.cases import load_cases  # noqa: E402
from review_bench.score import matches  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--criteria", type=Path, default=Path(__file__).resolve().parents[1] / "criteria.json")
    args = parser.parse_args(argv)
    min_hits = json.loads(args.criteria.read_text()).get("min_keyword_hits", 1)
    cases = {c["id"]: c for c in load_cases(args.cases)}
    rows = [json.loads(line) for line in args.results.read_text().splitlines() if line.strip()]
    print("=== HITS")
    for r in rows:
        if not r["valid"]:
            print(f"INVALID: {r['case']} {r['backend']} #{r['repeat']}: {(r.get('error') or '')[:160]} {r.get('problems')}")
            continue
        case = cases[r["case"]]
        for f in r["result"]["findings"]:
            for e in case["expected"]:
                if matches(f, e, min_hits):
                    print(f"- {r['case']} {r['backend']} #{r['repeat']} [{f['severity']}] -> {e['id']}")
                    print(f"    {f['summary'][:300]}")
    print("\n=== SPURIOUS BLOCKING")
    for r in rows:
        if not r["valid"]:
            continue
        case = cases[r["case"]]
        for f in r["result"]["findings"]:
            if f["severity"] == "blocking" and not any(matches(f, e, min_hits) for e in case["expected"]):
                print(f"- {r['case']} ({case['kind']}) {r['backend']} #{r['repeat']} {f['file']}")
                print(f"    {f['summary'][:300]}")
                print(f"    scenario: {f['failure_scenario'][:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
