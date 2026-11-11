"""Training data for DSPy, harvested from the Phase 3 eval suite and its traces.

1. `collect_traces()` runs the eval cases through the full orchestrator with a *recording* planner wrapper, so we
   capture exactly what the planner saw (goal + context: linked graph entities, memories) and what it returned.
   Only cases whose whole trajectory PASSED the Phase 3 grader are kept: the gold plan is trajectory-verified,
   not just "what the rules said". The verification records in each run's audit log give verifier examples.
2. `augment()` adds template variants (new numbers, ids, recipients, phrasings) labelled by the rule planner and
   linked against the real graph, so the optimiser sees more than one example per behaviour.
All data is fictional (mock CRM, sample PDF, *.example addresses).
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
from pathlib import Path

from ..planner import RuleBasedPlanner


class RecordingPlanner:
    def __init__(self, inner=None):
        self.inner = inner or RuleBasedPlanner()
        self.name = getattr(self.inner, "name", "rule-based")
        self.records: list[dict] = []

    def plan(self, goal: str, context: dict | None = None):
        steps = self.inner.plan(goal, context)
        self.records.append({"goal": goal, "context": context or {}, "steps": [dict(s) for s in steps]})
        return steps


def _clean_steps(steps: list[dict]) -> list[dict]:
    return [{"tool": s["tool"], "args": s.get("args", {})} for s in steps]


def collect_traces(cases: list[dict] | None = None, workdir: Path | None = None) -> dict:
    from ..evals.grader import grade
    from ..evals.runner import build_graph, load_cases, run_case
    os.environ.setdefault("LOG_ON_AS_OF", "2026-11-11")
    os.environ.setdefault("LOG_ON_TRACE_EXPORTERS", "jsonl")
    cases = cases if cases is not None else load_cases()
    work = Path(workdir or tempfile.mkdtemp(prefix="log_on_traces_"))
    graph = build_graph(work)
    plans, verifs, skipped = [], [], []
    for c in cases:
        if c.get("planner_steps") or not c["category"].startswith(("normal", "edge")):
            skipped.append(c["id"])
            continue
        rec = RecordingPlanner()
        traj = run_case(c, work, graph, planner=rec)
        g = grade(c, traj)
        if not g["passed"]:
            skipped.append(c["id"])
            continue
        main = [r for r in rec.records if r["goal"] == c["goal"]][:1]  # first attempt for the case's own goal
        for r in main:
            if any(p["goal"] == r["goal"] and p["context"] == r["context"] for p in plans):
                continue  # same goal re-used by several cases (e.g. retry / budget variants)
            plans.append({"id": c["id"], "category": c["category"], "goal": r["goal"], "context": r["context"],
                          "gold_steps": _clean_steps(r["steps"])})
        audit = work / c["id"] / "audit.jsonl"
        for line in audit.read_text().splitlines():
            row = json.loads(line)
            if row["node"] == "verification" and row["event"] == "completed":
                v = row["data"].get("verification") or {}
                if v.get("passed") is None or not str(v.get("verifier", "")).startswith("rule"):
                    continue
                att = (row["data"].get("attempts") or [{}])[-1]
                verifs.append({"id": c["id"], "goal": c["goal"], "plan": att.get("plan", []),
                               "results": att.get("results", []), "passed": bool(v["passed"]),
                               "reason": "; ".join(v.get("reasons", [])) or "goal satisfied"})
    return {"plans": plans, "verifications": verifs, "skipped": skipped, "graph": str(graph)}


TEMPLATES = [
    ("Calculate {a} * {b} + {c}", None), ("What is {a} + {b} - {c}?", None), ("Compute ({a} + {b}) / {c}", None),
    ("Look up customer C-{cid}", None), ("Show customer C-{cid} details", None), ("Find customer C-{cid}", None),
    ("Get the Q3 revenue report", None), ("Show me the weekly ops report", None), ("Fetch the weekly ops report", None),
    ("Send the Q3 revenue report to {who}@{org}.example", None), ("Email the weekly ops report to {who}@{org}.example", None),
    ("Share the Q3 revenue report with {who}@{org}.example", None),
    ("What's the weather in Lagos tomorrow?", None), ("Write me a poem about the sea", None), ("Book a flight to Abuja", None),
    ("Who supplies {customer}?", "graph"), ("Which suppliers serve {customer}?", "graph"),
    ("How is {customer} connected to {supplier}?", "graph"), ("Tell me about {supplier}", "graph"),
]
CUSTOMERS = ["Eko Pharma Distribution Ltd", "Zuma Cement Plc", "Sahel Telecom Towers", "Delta Agro Exports"]
SUPPLIERS = ["Onne Port Services", "Atlantic Reefers"]


def context_for(goal: str, graph_path: str | Path | None) -> dict:
    """The same context the orchestrator would build (entity linking against the read-only graph)."""
    if not graph_path:
        return {}
    from ..kg import KuzuGraphStore
    from ..kg.qa import link_entities
    with KuzuGraphStore(graph_path, read_only=True) as g:
        ents = [{**e, "from": "goal"} for e in link_entities(g, goal)]
    return {"entities": ents, "graph_available": True, "memories": [], "instructions": {}}


def augment(n: int = 40, seed: int = 7, graph_path: str | Path | None = None) -> list[dict]:
    rnd = random.Random(seed)
    rule, out, seen = RuleBasedPlanner(), [], set()
    tries = 0
    while len(out) < n and tries < n * 20:
        tries += 1
        tpl, kind = rnd.choice(TEMPLATES)
        if kind == "graph" and not graph_path:
            continue
        goal = tpl.format(a=rnd.randint(2, 999), b=rnd.randint(2, 99), c=rnd.randint(2, 50), cid=rnd.randint(1001, 1003),
                          who=rnd.choice(["finance", "ops", "audit", "board"]), org=rnd.choice(["acme", "lagoon"]),
                          customer=rnd.choice(CUSTOMERS), supplier=rnd.choice(SUPPLIERS))
        if goal in seen:
            continue
        seen.add(goal)
        ctx = context_for(goal, graph_path) if kind == "graph" else {}
        out.append({"id": "aug-" + hashlib.sha1(goal.encode()).hexdigest()[:6], "category": "augmented",
                    "goal": goal, "context": ctx, "gold_steps": _clean_steps(rule.plan(goal, ctx))})
    return out


def split(examples: list[dict], dev_frac: float = 0.4, seed: int = 0) -> tuple[list[dict], list[dict]]:
    """Deterministic split by hashed id (stable across runs and machines)."""
    def h(e):
        return int(hashlib.sha1(f"{seed}:{e['id']}".encode()).hexdigest(), 16) % 1000 / 1000
    train = [e for e in examples if h(e) >= dev_frac]
    dev = [e for e in examples if h(e) < dev_frac]
    return train, dev


def to_dspy(examples: list[dict]):
    import dspy

    from .signatures import TOOLS_DOC, context_str
    return [dspy.Example(goal=e["goal"], context=context_str(e["context"]), tools=TOOLS_DOC,
                         gold_steps=json.dumps(e["gold_steps"]), steps_json=json.dumps(e["gold_steps"]),
                         id=e["id"]).with_inputs("goal", "context", "tools") for e in examples]


def to_dspy_verify(examples: list[dict]):
    import dspy
    return [dspy.Example(goal=e["goal"], plan_json=json.dumps(e["plan"]), results_json=json.dumps(e["results"]),
                         passed=e["passed"], reason=e["reason"], id=e["id"]).with_inputs("goal", "plan_json", "results_json")
            for e in examples]
