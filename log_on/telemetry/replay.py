"""Replay / inspect a run from the local JSONL trace (and the audit log for context).

  python -m log_on.trace --list                       # recent runs: status, spans, latency, tokens
  python -m log_on.trace <run_id>                     # span tree with latencies, decisions, tool calls
  python -m log_on.trace <run_id> --json              # raw spans for that run
  python -m log_on.trace <run_id> --file runs/traces.jsonl
A run that paused for a human has several traces (run.start, run.resume ...); they are shown in order.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_spans(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def spans_for(spans: list[dict], run_id: str) -> list[dict]:
    """All spans of every trace whose root carries this run id (prefix match allowed)."""
    traces = {s["trace_id"] for s in spans if str(s["attributes"].get("log_on.run_id", "")).startswith(run_id)}
    return sorted((s for s in spans if s["trace_id"] in traces), key=lambda s: s["start_ns"] or 0)


def list_runs(spans: list[dict]) -> list[dict]:
    runs: dict[str, dict[str, Any]] = {}
    for s in sorted(spans, key=lambda s: s["start_ns"] or 0):
        if not s["name"].startswith("run."):
            continue
        a = s["attributes"]
        r = runs.setdefault(a.get("log_on.run_id", "?"), {"run_id": a.get("log_on.run_id", "?"), "invocations": 0,
                                                          "duration_ms": 0.0, "goal": a.get("log_on.goal", "")})
        r["invocations"] += 1
        r["duration_ms"] = round(r["duration_ms"] + s["duration_ms"], 1)
        r["status"] = a.get("log_on.status")
        r["tokens"] = a.get("log_on.usage.total_tokens")
        r["cost_usd"] = a.get("log_on.usage.est_cost_usd")
        r["steps"] = a.get("log_on.usage.steps")
        r["start_ns"] = r.get("start_ns") or s["start_ns"]
    return list(runs.values())


SHOW_ATTRS = ("status", "planner", "plan.tools", "decision.allowed", "decision.rule_ids", "decision.needs_approval",
              "decision.approved", "decision.escalation", "guard.violations", "verify.passed", "verify.score",
              "verify.reasons", "budget.exceeded", "ok", "args", "output_preview", "event")


def _fmt_attrs(a: dict) -> str:
    parts = []
    for k in SHOW_ATTRS:
        v = a.get(f"log_on.{k}")
        if v in (None, [], ""):
            continue
        if k == "output_preview":
            v = str(v).replace("\n", " ")[:90]
        parts.append(f"{k}={v}")
    return "  ".join(parts)


def render(spans: list[dict], run_id: str) -> str:
    sel = spans_for(spans, run_id)
    if not sel:
        return f"No trace found for run {run_id!r}."
    children = defaultdict(list)
    for s in sel:
        children[s["parent_span_id"]].append(s)
    lines: list[str] = []
    node_ms: dict[str, float] = defaultdict(float)
    tool_ms: dict[str, float] = defaultdict(float)

    def walk(s: dict, depth: int) -> None:
        a = s["attributes"]
        tag = "ERROR " if s["status"] == "ERROR" else ""
        lines.append(f"{'  ' * depth}{'└─ ' if depth else ''}{s['name']:<22} {s['duration_ms']:>9.1f} ms  {tag}{_fmt_attrs(a)}")
        if s["name"].startswith("node."):
            node_ms[s["name"][5:]] += s["duration_ms"]
        if s["name"].startswith("tool."):
            tool_ms[s["name"][5:]] += s["duration_ms"]
        for e in s.get("events", []):
            ea = e["attributes"]
            if e["name"] == "route":
                lines.append(f"{'  ' * (depth + 1)}   ↳ route {ea.get('from')} → {ea.get('to')}  (status={ea.get('status')})")
        for c in sorted(children.get(s["span_id"], []), key=lambda x: x["start_ns"] or 0):
            walk(c, depth + 1)

    roots = [s for s in sel if s["parent_span_id"] is None]
    for i, r in enumerate(roots, 1):
        a = r["attributes"]
        lines.append(f"=== trace {i}/{len(roots)}  {r['name']}  trace_id={r['trace_id'][:16]}…  run={a.get('log_on.run_id')}")
        if a.get("log_on.goal"):
            lines.append(f"goal: {a['log_on.goal']}")
        walk(r, 0)
        lines.append(f"→ status={a.get('log_on.status')} steps={a.get('log_on.usage.steps')} "
                     f"tool_calls={a.get('log_on.usage.tool_calls')} tokens={a.get('log_on.usage.total_tokens')} "
                     f"est_cost=${a.get('log_on.usage.est_cost_usd')}")
    lines.append("\nlatency by node (ms): " + ", ".join(f"{k}={v:.1f}" for k, v in sorted(node_ms.items(), key=lambda x: -x[1])))
    if tool_ms:
        lines.append("latency by tool (ms): " + ", ".join(f"{k}={v:.1f}" for k, v in sorted(tool_ms.items(), key=lambda x: -x[1])))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m log_on.trace", description="Inspect LOG_ON run traces")
    ap.add_argument("run_id", nargs="?")
    ap.add_argument("--file", default="runs/traces.jsonl")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    a = ap.parse_args(argv)
    spans = load_spans(a.file)
    if not spans:
        print(f"No spans in {a.file}. Run something first, e.g. python -m log_on \"Calculate 2*21\"")
        return 1
    if a.list or not a.run_id:
        runs = list_runs(spans)[-a.limit:]
        print(f"{'run_id':<14}{'status':<18}{'inv':>4}{'steps':>6}{'tokens':>8}{'ms':>10}  goal")
        for r in runs:
            print(f"{r['run_id']:<14}{str(r.get('status')):<18}{r['invocations']:>4}{str(r.get('steps')):>6}"
                  f"{str(r.get('tokens')):>8}{r['duration_ms']:>10.1f}  {str(r['goal'])[:60]}")
        return 0
    if a.json:
        print(json.dumps(spans_for(spans, a.run_id), indent=2))
        return 0
    print(render(spans, a.run_id))
    return 0
