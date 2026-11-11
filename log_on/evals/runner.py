"""Run the eval dataset through the full Phase 1-3 orchestrator and collect each case's trajectory.

Every case gets a fresh working directory (audit log, outbox, traces, memory DB) and the shared,
read-only knowledge graph built once from the sample PDF. Human steps are scripted per case
(`human: {approval: approve|deny, escalation: accept|abort|retry, retry_goal: ...}`); the default
for both is the safe answer (deny / abort).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import yaml

from ..permissions import TOOL_POLICY

DEFAULT_CASES = Path(__file__).resolve().parents[2] / "evals" / "cases.yaml"


class ScriptedPlanner:
    """Simulates a compromised or buggy LLM planner: returns exactly the steps in the case file."""
    name = "scripted"

    def __init__(self, steps: list[dict]):
        self.steps = steps

    def plan(self, goal: str, context: dict | None = None) -> list[dict]:
        return [dict(s) for s in self.steps]


def load_cases(path: str | Path = DEFAULT_CASES) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cases = data["cases"] if isinstance(data, dict) else data
    ids = [c["id"] for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate case ids: {sorted(dupes)}")
    for c in cases:
        if "goal_repeat" in c:
            c["goal"] = c["goal_repeat"]["text"] * int(c["goal_repeat"]["times"])
    return cases


def build_graph(workdir: Path) -> Path:
    from ..docai import ingest_pdf
    from ..kg import KuzuGraphStore, RuleBasedExtractor, load_into
    pdf = Path(__file__).resolve().parents[2] / "samples" / "lagoon_freight_portfolio_fy2026.pdf"
    path = workdir / "graph.kuzu"
    if not path.exists():
        ex = RuleBasedExtractor().extract(ingest_pdf(pdf))
        with KuzuGraphStore(path) as s:
            load_into(s, ex)
    return path


def _read(p: Path) -> str:
    if not p.exists():
        return ""
    return p.read_bytes().decode("utf-8", "replace")


def run_case(case: dict, workdir: Path, graph_path: Path) -> dict[str, Any]:
    from ..graph import Orchestrator
    from ..loop import LoopConfig
    from ..memory import MemoryManager, make_memory_store
    from ..planner import RuleBasedPlanner
    from ..telemetry.cost import Budget

    d = workdir / case["id"]
    d.mkdir(parents=True, exist_ok=True)
    planner = ScriptedPlanner(case["planner_steps"]) if case.get("planner_steps") else RuleBasedPlanner()
    store, emb = make_memory_store(d / "memory.sqlite", os.environ.get("LOG_ON_EVAL_EMBEDDINGS", "hash"))
    loop = LoopConfig(max_attempts=int(case.get("max_attempts", 3)), budget=Budget(**case.get("budget", {})))
    orch = Orchestrator(planner=planner, audit_path=d / "audit.jsonl", outbox=d / "outbox.jsonl",
                        memory=MemoryManager(store, emb), graph_path=graph_path, loop=loop,
                        trace_path=d / "traces.jsonl")
    for g in case.get("setup", []):
        orch.start(g, user=case.get("user", "rhonda"))
    human = case.get("human", {})
    t0 = time.perf_counter()
    res = orch.start(case["goal"], user=case.get("user", "rhonda"))
    for _ in range(5):  # scripted human-in-the-loop
        if res["status"] == "awaiting_approval":
            res = orch.resume(res["run_id"], approved=human.get("approval") == "approve", approver="eval")
        elif res["status"] == "awaiting_human":
            res = orch.respond(res["run_id"], human.get("escalation", "abort"), goal=human.get("retry_goal"), by="eval")
        else:
            break
    latency = round((time.perf_counter() - t0) * 1000, 1)
    return trajectory(orch, res, d, latency)


def trajectory(orch, res: dict, d: Path, latency_ms: float) -> dict[str, Any]:
    rows = orch.audit.read(res["run_id"])
    tools: list[str] = []
    approved_so_far = False
    gated_before_approval: list[str] = []
    for r in rows:
        if r["node"] == "approval" and r["event"] == "completed" and r["data"].get("approval", {}).get("approved"):
            approved_so_far = True
        if r["node"] == "action" and r["event"] == "completed":
            for t in r["data"].get("tool_results", []):
                if t.get("skipped"):  # fail-fast: never executed
                    continue
                tools.append(t["tool"])
                if TOOL_POLICY.get(t["tool"], {}).get("approval") and not approved_so_far:
                    gated_before_approval.append(t["tool"])
            approved_so_far = False  # every executed plan needs its own approval
        if r["node"] == "validation" and r["event"] == "completed":
            tools += [v["tool"] for v in r["data"].get("validated_results", []) if v["tool"] == "memory"]
    events = [(r["node"], r["event"]) for r in rows]
    sent = [json.loads(line) for line in _read(d / "outbox.jsonl").splitlines() if line.strip()]
    corpus = "\n".join([res.get("final_output") or "", _read(d / "outbox.jsonl"), _read(d / "audit.jsonl"),
                        _read(d / "traces.jsonl")]
                       + [_read(p) for p in d.glob("memory.sqlite*")])
    return {"run_id": res["run_id"], "status": res["status"], "final_output": res.get("final_output") or "",
            "tools": tools, "events": events, "policy_rules": (res.get("policy") or {}).get("rule_ids", []),
            "approval_requested": ("approval", "interrupted") in events,
            "escalated": ("escalation", "interrupted") in events, "gated_before_approval": gated_before_approval,
            "attempts": len(res.get("attempts", [])) or 0, "steps": (res.get("usage") or {}).get("steps", 0),
            "usage": res.get("usage") or {}, "sent": sent, "leak_corpus": corpus, "latency_ms": latency_ms}
