"""python -m log_on.evals [--cases evals/cases.yaml] [--thresholds evals/thresholds.json] [--out report.json]
                         [--only ID,ID] [--category attack]
                         [--planner rule|llm --provider groq --model M --base-url U]
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


def crashed(c: dict, exc: Exception) -> dict:
    return {"id": c["id"], "category": c["category"], "score": 0.0, "passed": False,
            "metrics": {k: 0.0 for k in ("tool_selection", "step_order", "policy_compliance", "approval",
                                         "output", "efficiency")},
            "notes": {"error": f"{type(exc).__name__}: {exc}"}, "leaks": 0, "status": "crashed", "tools": [],
            "steps": 0, "attempts": 0, "tokens": 0, "cost_usd": 0.0, "latency_ms": 0}


def run_suite(cases: list[dict], work: Path, graph: Path, planner_factory=None, log=print,
              grading: str = "route") -> list[dict]:
    """Grade every case. `planner_factory()` builds a fresh planner per case (None = RuleBasedPlanner);
    scripted `planner_steps` cases always use their script."""
    results = []
    log(f"{'id':<6}{'category':<26}{'score':>6} {'pass':<5}{'status':<17}{'steps':>5}{'att':>4}  tools / notes")
    for c in cases:
        try:
            r = grade(c, run_case(c, work, graph, planner=planner_factory() if planner_factory else None), grading)
        except Exception as exc:  # noqa: BLE001 - a crashing case is a failing case, not a crashed suite
            r = crashed(c, exc)
        results.append(r)
        bad = "; ".join(f"{k}: {v}" for k, v in r["notes"].items() if k == "error" or r["metrics"].get(k, 1) < 1)
        log(f"{r['id']:<6}{r['category']:<26}{r['score']:>6.3f} {'PASS' if r['passed'] else 'FAIL':<5}"
            f"{r['status']:<17}{r['steps']:>5}{r['attempts']:>4}  {r['tools']}" + (f"  <- {bad}" if bad else ""))
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m log_on.evals")
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument("--thresholds", default=str(Path(DEFAULT_CASES).with_name("thresholds.json")))
    ap.add_argument("--out", help="write the JSON report here")
    ap.add_argument("--workdir", help="keep per-case audit/trace/outbox files here (default: temp dir)")
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--category", help="run only categories starting with this prefix")
    ap.add_argument("--planner", default="rule", choices=["rule", "llm"],
                    help="llm: plan with a served/hosted model (falls back to rules per call on errors)")
    ap.add_argument("--provider", default=None, help="provider preset for --planner llm (default: LOG_ON_LLM_PROVIDER "
                    "or llamacpp), e.g. groq, groq-qwen, openrouter, openrouter-free")
    ap.add_argument("--model")
    ap.add_argument("--base-url")
    ap.add_argument("--grading", choices=["route", "outcome"], default=None,
                    help="route: exact rule-planner trajectory (default for --planner rule); outcome: safety "
                         "invariants + acceptable outcomes (default for --planner llm). See log_on/evals/grader.py")
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

    factory, meter = None, None
    if a.planner == "llm":
        from ..serving import make_provider
        from ..serving.compare import Meter, MeteredLLMPlanner
        prov = make_provider(a.provider or os.environ.get("LOG_ON_LLM_PROVIDER", "llamacpp"), base_url=a.base_url,
                             model=a.model, timeout=300)
        if prov.config.name in ("groq", "groq-qwen", "openrouter", "openrouter-free") and not prov.config.api_key:
            print(f"--planner llm: no API key for {prov.config.name} (set its key env var)", file=sys.stderr)
            return 2
        meter = Meter()
        factory = lambda: MeteredLLMPlanner(prov, meter)  # noqa: E731
        print(f"planner: llm on {prov.name}:{prov.model}", flush=True)
    grading = a.grading or ("outcome" if a.planner == "llm" else "route")
    print(f"grading: {grading}", flush=True)
    results = run_suite(cases, work, graph, factory, log=lambda m: print(m, flush=True), grading=grading)

    summary = summarise(results)
    if meter is not None:
        summary["planner_llm"] = meter.summary()
        print("planner llm:", json.dumps(summary["planner_llm"]))
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
