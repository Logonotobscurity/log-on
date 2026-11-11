"""python -m log_on.evals [--cases evals/cases.yaml] [--thresholds evals/thresholds.json] [--out report.json]
                         [--only ID,ID] [--category attack]
Runs every case, prints a per-case table and the suite summary, and exits 1 if any threshold fails (CI gate)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .grader import check_thresholds, grade, summarise
from .runner import DEFAULT_CASES, build_graph, load_cases, run_case


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m log_on.evals")
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument("--thresholds", default=str(Path(DEFAULT_CASES).with_name("thresholds.json")))
    ap.add_argument("--out", help="write the JSON report here")
    ap.add_argument("--workdir", help="keep per-case audit/trace/outbox files here (default: temp dir)")
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--category", help="run only categories starting with this prefix")
    a = ap.parse_args(argv)

    os.environ.setdefault("LOG_ON_AS_OF", "2026-11-11")  # graph quarter questions are relative to "today"
    os.environ.setdefault("LOG_ON_TRACE_EXPORTERS", "jsonl")
    cases = load_cases(a.cases)
    if a.only:
        keep = set(a.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    if a.category:
        cases = [c for c in cases if c["category"].startswith(a.category)]
    work = Path(a.workdir or tempfile.mkdtemp(prefix="log_on_evals_"))
    work.mkdir(parents=True, exist_ok=True)
    graph = build_graph(work)

    results = []
    print(f"{'id':<6}{'category':<26}{'score':>6} {'pass':<5}{'status':<17}{'steps':>5}{'att':>4}  tools / notes")
    for c in cases:
        try:
            r = grade(c, run_case(c, work, graph))
        except Exception as exc:  # noqa: BLE001 - a crashing case is a failing case, not a crashed suite
            r = {"id": c["id"], "category": c["category"], "score": 0.0, "passed": False,
                 "metrics": {k: 0.0 for k in ("tool_selection", "step_order", "policy_compliance", "approval",
                                              "output", "efficiency")},
                 "notes": {"error": f"{type(exc).__name__}: {exc}"}, "leaks": 0, "status": "crashed", "tools": [],
                 "steps": 0, "attempts": 0, "tokens": 0, "cost_usd": 0.0, "latency_ms": 0}
        results.append(r)
        bad = "; ".join(f"{k}: {v}" for k, v in r["notes"].items() if k == "error" or r["metrics"].get(k, 1) < 1)
        print(f"{r['id']:<6}{r['category']:<26}{r['score']:>6.3f} {'PASS' if r['passed'] else 'FAIL':<5}"
              f"{r['status']:<17}{r['steps']:>5}{r['attempts']:>4}  {r['tools']}" + (f"  <- {bad}" if bad else ""))

    summary = summarise(results)
    thresholds = json.loads(Path(a.thresholds).read_text()) if Path(a.thresholds).exists() else {}
    fails = check_thresholds(summary, thresholds) if not (a.only or a.category) else []
    print("\nsummary:", json.dumps(summary, indent=2))
    print("thresholds:", json.dumps(thresholds))
    if a.out:
        Path(a.out).write_text(json.dumps({"summary": summary, "thresholds": thresholds, "threshold_failures": fails,
                                           "results": results}, indent=2))
    if fails:
        print("EVAL GATE FAILED:\n  " + "\n  ".join(fails))
        return 1
    print("EVAL GATE PASSED" if thresholds and not (a.only or a.category) else "(thresholds not checked for a subset)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
