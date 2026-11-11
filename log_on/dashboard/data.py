"""Pure data loaders for the LOG_ON dashboard. Everything here reads real files: the runs directory the approval
service is attached to (audit.jsonl, feedback.jsonl, traces.jsonl, quota store) and the committed evidence in docs/
and prompts/. Nothing is invented: a number the files do not contain is returned as None and rendered as "no data".

Free text from runs is passed through the same redactor the audit log uses before it leaves this module, as
defence in depth (the audit log already stores redacted text)."""
from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from ..audit import AuditLog
from ..guards.validators import redact_text

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
DEMO_MARKER = "DEMO_DATA.txt"

# ---------------------------------------------------------------- the 13 layers and how LOG_ON's graph maps onto them
# (layer, graph nodes, honest note). The reference architecture lists the layers in this order; LOG_ON's graph runs
# policy before memory retrieval and approval before action, and merges execution into the action node.
LAYERS = [
    ("intake", ["intake"], "Request accepted, run id and per-run budget assigned."),
    ("memory", ["memory_retrieval", "knowledge_retrieval"], "Long-term memory and the knowledge graph are read. "
     "In LOG_ON this runs after the policy check, so blocked requests never touch memory."),
    ("policy", ["policy"], "Input policy: rules plus the ML injection classifier."),
    ("orchestration", ["quota_stop", "budget_stop"], "LangGraph routing between nodes; it only has its own node "
     "when it stops a run (daily quota or run budget)."),
    ("planning", ["planning", "replan"], "Planner proposes a tool plan; replan after a failed verification."),
    ("permission", ["permission"], "Plan checked against tool allow-lists, data-flow and provenance rules."),
    ("action", ["action"], "Tools are called."),
    ("validation", ["validation"], "Output guards: PII redaction, schema and leak checks."),
    ("approval", ["approval", "approval_decision"], "Human approval gate for high-risk steps. In LOG_ON it runs "
     "before action, not after validation."),
    ("execution", ["action"], "No separate executor: execution shares the action node (no sandbox boundary)."),
    ("audit", ["audit"], "Run finalised into the hash-chained audit log."),
    ("evaluation", ["verification", "escalation"], "Self-check of results; failures replan or escalate to a human."),
    ("learning", ["memory_update"], "Memory written; approver notes go to the feedback log for the learning loop."),
]
BLOCKING = {"refused", "denied", "blocked", "no_plan", "quota_exceeded", "budget_exceeded", "failed", "error",
            "expired"}
PAUSED = {"awaiting_approval": "paused", "awaiting_human": "escalated"}


def _redact(s) -> str:
    return redact_text(str(s)) if s is not None else ""


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, round(p / 100 * (len(xs) - 1))))
    return round(xs[k], 2)


def is_demo(runs_dir: Path) -> bool:
    return (Path(runs_dir) / DEMO_MARKER).exists()


# ---------------------------------------------------------------- runs and the journey
def runs(runs_dir: Path) -> list[dict]:
    """One summary per graph run found in audit.jsonl, newest first."""
    recs = _jsonl(Path(runs_dir) / "audit.jsonl")
    by: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        by[r.get("run_id", "")].append(r)
    out = []
    for rid, rs in by.items():
        intake = next((r for r in rs if r.get("node") == "intake"), None)
        if intake is None:
            continue  # ingest jobs and other non-graph records
        d = intake.get("data", {})
        status, usage, decisions = d.get("status", "running"), {}, []
        for r in rs:
            data = r.get("data", {}) or {}
            if r.get("event") == "interrupted":
                status = "awaiting_approval" if r["node"] == "approval" else "awaiting_human"
            elif data.get("status"):
                status = data["status"]
            if isinstance(data.get("usage"), dict):
                usage = data["usage"]
            if r.get("node") == "approval_decision":
                decisions.append({"decision": data.get("decision"), "approver": data.get("approver"),
                                  "note": _redact(data.get("note", "")), "source": data.get("source"),
                                  "ts": r.get("ts")})
        if status == "running" and any(r.get("node") == "audit" for r in rs):
            status = "completed"
        out.append({"run_id": rid, "user": d.get("user"), "goal": _redact(d.get("goal", "")), "status": status,
                    "started": rs[0].get("ts"), "ended": rs[-1].get("ts"), "records": len(rs),
                    "usage": usage, "decisions": decisions,
                    "nodes": [r.get("node") for r in rs]})
    out.sort(key=lambda r: r["started"] or "", reverse=True)
    return out


def _node_detail(node: str, data: dict) -> str:
    """A short, redacted, human sentence for what a node did."""
    if node == "policy":
        p = data.get("policy", {})
        if p.get("allowed", True):
            return "allowed" + (f" (ML score {p.get('ml_score'):.2f})" if p.get("ml_score") is not None else "")
        return "refused: " + ", ".join(p.get("rule_ids", []))
    if node == "planning":
        plan = data.get("plan") or []
        return f"{data.get('planner', '?')} planner: " + (" → ".join(s.get("tool", "?") for s in plan) or "no plan")
    if node == "permission":
        p = data.get("permission", {})
        if p.get("needs_approval"):
            return f"needs approval for steps {p.get('approval_steps')}"
        return "allowed" if p.get("allowed", True) else "denied: " + ", ".join(p.get("denied_tools", []) or ["rule"])
    if node == "action":
        trs = data.get("tool_results") or []
        return ", ".join(f"{t.get('tool')} {'ok' if t.get('ok') else 'failed'}" for t in trs) or "no tool calls"
    if node == "validation":
        v = data.get("violations") or []
        return f"{len(v)} violation(s)" if v else "clean"
    if node == "verification":
        v = data.get("verification", {}) or {}
        return "verified" if v.get("ok", v.get("passed", True)) else "failed: " + _redact(v.get("reason", ""))[:80]
    if node in ("approval", "escalation"):
        if "reason" in data:
            return _redact(data["reason"])[:140]
        a = data.get("approval", {}) or {}
        return f"{a.get('decision', data.get('status', ''))} by {a.get('approver', '?')}" if a else str(data.get("status", ""))
    if node == "approval_decision":
        return f"{data.get('decision')} by {data.get('approver')} via {data.get('source')}"
    if node == "quota_stop":
        return _redact(data.get("final_output", "daily quota exceeded"))[:140]
    if node == "memory_retrieval":
        m = data.get("memory") or {}
        n = len(m.get("items", m)) if isinstance(m, (dict, list)) else 0
        return f"{n} item(s) recalled"
    if node == "knowledge_retrieval":
        k = data.get("knowledge") or {}
        return "graph queried" if k else "no graph context"
    if node == "memory_update":
        return f"{len(data.get('memory_writes') or [])} memory write(s)"
    if node == "replan":
        return f"revision {data.get('revision', '?')}"
    return str(data.get("status", "")) or "done"


def journey(runs_dir: Path, run_id: str) -> dict | None:
    """The 13-layer journey of one run, from its audit records."""
    rs = [r for r in _jsonl(Path(runs_dir) / "audit.jsonl") if r.get("run_id") == run_id]
    if not rs or not any(r.get("node") == "intake" for r in rs):
        return None
    summary = next(r for r in runs(runs_dir) if r["run_id"] == run_id)
    final = summary["status"]
    steps = []
    for i, r in enumerate(rs):
        data = r.get("data", {}) or {}
        steps.append({"i": i + 1, "ts": r.get("ts"), "node": r.get("node"), "event": r.get("event"),
                      "status": data.get("status"), "detail": _node_detail(r.get("node"), data),
                      "hash": (r.get("hash") or "")[:12], "prov": r.get("prov")})
    visited = {s["node"] for s in steps}
    # where the run stopped (last meaningful node, not the finaliser)
    stop = next((s for s in reversed(steps) if s["node"] not in ("audit",)), steps[-1])
    stop_state = ("blocked" if final in BLOCKING else PAUSED.get(final)) or None
    layers = []
    for idx, (name, nodes, note) in enumerate(LAYERS, 1):
        hit = [n for n in nodes if n in visited]
        if hit:
            state = "passed"
            if stop_state and stop["node"] in nodes:
                state = stop_state
            elif name != "audit" and any(s["node"] in nodes and s["status"] in BLOCKING for s in steps):
                state = "blocked"
        else:
            state = "not reached" if final in BLOCKING or final in PAUSED else "not needed"
        if name == "orchestration" and not hit and len(visited) > 1:
            state = "passed"  # routing happened (implicitly) whenever more than intake ran
        if name == "learning" and not hit and summary["decisions"] and final not in PAUSED:
            state = "passed"
        layers.append({"n": idx, "layer": name, "nodes": nodes, "hit": hit, "state": state, "note": note,
                       "detail": "; ".join(s["detail"] for s in steps if s["node"] in hit)[:300]})
    return {"run": summary, "layers": layers, "steps": steps,
            "stopped_at": stop["node"], "outcome": final}


# ---------------------------------------------------------------- audit chain
def audit(runs_dir: Path, limit: int = 400) -> dict:
    out = {}
    for name in ("audit.jsonl", "feedback.jsonl"):
        p = Path(runs_dir) / name
        if not p.exists():
            out[name] = {"exists": False}
            continue
        rep = AuditLog(p).verify_report()
        out[name] = {"exists": True, "ok": rep["ok"], "records": rep["records"],
                     "legacy_unstamped": rep.get("legacy_unstamped", 0), "errors": rep.get("errors", [])[:10],
                     "versions": rep.get("versions", {})}
    recs = _jsonl(Path(runs_dir) / "audit.jsonl")
    rows = []
    for r in recs[-limit:][::-1]:
        data = r.get("data", {}) or {}
        prov = r.get("prov") or {}
        rows.append({"ts": r.get("ts"), "run_id": r.get("run_id"), "node": r.get("node"), "event": r.get("event"),
                     "status": data.get("status"), "detail": _node_detail(r.get("node"), data) if r.get("node") != "ingest"
                     else f"document ingested ({data.get('chunks', '?')} chunks)",
                     "hash": (r.get("hash") or "")[:16], "prev": (r.get("prev_hash") or "")[:16],
                     "code": (prov.get("code") or "")[:12] + ("-dirty" if str(prov.get("code", "")).endswith("-dirty") else ""),
                     "policy_version": prov.get("policy_version"), "policy_hash": prov.get("policy_hash"),
                     "planner": prov.get("planner"), "model": prov.get("model")})
    out["rows"] = rows
    out["nodes"] = sorted({r["node"] for r in rows if r["node"]})
    out["provenance"] = [{"stamp": k, "records": v} for k, v in
                         Counter(f"{r['policy_version']} · policy {r['policy_hash']} · code {r['code']} · "
                                 f"{r['planner']}/{r['model']}" for r in rows if r["policy_version"]).most_common()]
    return out


# ---------------------------------------------------------------- evals and safety evidence (docs/)
def _summary_block(text: str) -> dict | None:
    i = text.rfind("summary: {")
    if i < 0:
        return None
    dec = json.JSONDecoder()
    try:
        return dec.raw_decode(text[i + len("summary: "):])[0]
    except json.JSONDecodeError:
        return None


def evals(docs: Path = DOCS) -> dict:
    hist = []
    for label, fn in [("Phase 3", "eval_report_phase3.txt"), ("Phase 4", "eval_report_phase4.txt"),
                      ("Step 2 (ML safety)", "eval_report_safety_ml.txt"), ("Step 3 (production)", "eval_report_step3.txt")]:
        p = docs / fn
        if not p.exists():
            continue
        t = p.read_text(encoding="utf-8", errors="replace")
        s = _summary_block(t)
        if s:
            hist.append({"label": label, "file": f"docs/{fn}", "cases": s.get("cases"), "pass_rate": s.get("pass_rate"),
                         "attack_pass_rate": s.get("attack_pass_rate"), "pii_leaks": s.get("pii_leaks"),
                         "overall_score": s.get("overall_score"), "metrics": s.get("metrics", {}),
                         "by_category": s.get("by_category", {}), "failed": s.get("failed", []),
                         "gate": "PASSED" if "EVAL GATE PASSED" in t else ("FAILED" if "EVAL GATE FAILED" in t else "unknown"),
                         "thresholds": _thresholds(t)})
    return {"history": hist, "latest": hist[-1] if hist else None}


def _thresholds(t: str) -> dict | None:
    m = re.search(r"^thresholds: (\{.*\})$", t, re.M)
    try:
        return json.loads(m.group(1)) if m else None
    except json.JSONDecodeError:
        return None


REDTEAM = [("Phase 4 · rule planner · before fixes", "rule_before_fixes.json"),
           ("Phase 4 · rule planner · after fixes", "rule_after_fixes.json"),
           ("Phase 4 · Qwen 1.5B planner · final", "llm_qwen1.5b_final.json"),
           ("Step 2 · rule planner · before ML", "step2_rule_before.json"),
           ("Step 2 · rule planner · after ML", "step2_rule_after.json"),
           ("Step 2 · Qwen 1.5B planner · after ML", "step2_llm_qwen1.5b_after.json")]


def redteam(docs: Path = DOCS) -> dict:
    out = []
    for label, fn in REDTEAM:
        p = docs / "redteam" / fn
        if not p.exists():
            continue
        r = json.loads(p.read_text())
        s = r.get("summary", {})
        res = r.get("results", [])
        out.append({"label": label, "file": f"docs/redteam/{fn}", "attacks": s.get("attacks"),
                    "vulnerable": s.get("vulnerable"), "bypass": s.get("bypass_contained"), "defended": s.get("defended"),
                    "success_rate": s.get("attack_success_rate"), "planner": r.get("planner"),
                    "elapsed_s": r.get("elapsed_s"), "by_category": s.get("by_category", {}),
                    "by_technique": s.get("by_technique", {}),
                    "seeds": len({x.get("seed") for x in res if x.get("seed")}),
                    "techniques": len({x.get("technique") for x in res if x.get("technique")})})
    return {"runs": out, "partial_note": "The step-2 live-model 'before' run hit its 25-minute limit: at least 4 "
            "vulnerable and 120 contained bypasses in about 420 attacks (docs/safety_ml_report.md; no JSON was saved)."}


def safety_bench(docs: Path = DOCS) -> dict:
    p = docs / "safety_bench.json"
    if not p.exists():
        return {"modes": []}
    modes = []
    for m in json.loads(p.read_text()):
        pii, inj, ho, qu = m.get("pii", {}), m.get("injection", {}), m.get("heldout", {}), m.get("quarantine", {})
        modes.append({"mode": m.get("mode"), "classifier": (m.get("classifier") or {}).get("model"),
                      "pii_found": pii.get("found"), "pii_expected": pii.get("expected"),
                      "pii_per_entity": pii.get("per_entity", {}), "pii_misses": len(pii.get("misses", [])),
                      "inj_attacks": inj.get("attacks"), "inj_detected": inj.get("detected"),
                      "benign": inj.get("benign"), "benign_fp": inj.get("false_positives"),
                      "fp_examples": [e.get("text") for e in inj.get("fp_examples", [])],
                      "heldout": ho.get("attacks"), "heldout_detected": ho.get("detected"),
                      "heldout_missed": [_redact(x) for x in ho.get("missed", [])],
                      "untrusted": ho.get("untrusted"), "untrusted_quarantined": ho.get("untrusted_quarantined"),
                      "quarantine_benign": qu.get("benign_tool_outputs"), "quarantine_fp": qu.get("false_positives"),
                      "quarantine_fp_examples": [e.get("text") for e in qu.get("fp_examples", [])],
                      "lat_input_p50": (inj.get("latency_benign") or {}).get("p50_ms"),
                      "lat_input_p95": (inj.get("latency_benign") or {}).get("p95_ms"),
                      "lat_pii_p50": (pii.get("latency_negative") or {}).get("p50_ms"),
                      "lat_quarantine_p50": (qu.get("latency") or {}).get("p50_ms"),
                      "warmup_ms": m.get("warmup_ms")})
    return {"modes": modes, "file": "docs/safety_bench.json"}


# ---------------------------------------------------------------- ops
def serving(docs: Path = DOCS) -> list[dict]:
    out = []
    for p in sorted((docs / "benchmarks").glob("*.json")) if (docs / "benchmarks").is_dir() else []:
        b = json.loads(p.read_text())
        prov = b.get("provider", {})
        out.append({"file": f"docs/benchmarks/{p.name}", "model": prov.get("model"), "engine": prov.get("name"),
                    "date": b.get("date"), "cpus": (b.get("host") or {}).get("cpus"),
                    "levels": [{k: lv.get(k) for k in ("concurrency", "requests", "ok", "req_per_s", "output_tok_per_s",
                                                       "latency_p50_ms", "latency_p95_ms", "ttft_p50_ms",
                                                       "valid_plan_rate")} for lv in b.get("levels", [])]})
    return out


def dspy(root: Path = ROOT) -> dict:
    reg = root / "prompts" / "registry.json"
    out = {"active": None, "versions": []}
    if reg.exists():
        comp = json.loads(reg.read_text()).get("components", {}).get("planner", {})
        out["active"] = comp.get("active")
        for vid, v in comp.get("versions", {}).items():
            g = v.get("eval_gate", {})
            out["versions"].append({"id": vid, "status": v.get("status"), "created": v.get("created"),
                                    "model": v.get("model"), "optimizer": v.get("optimizer"),
                                    "scores": v.get("scores", {}), "gate_passed": g.get("passed"),
                                    "failures": g.get("failures", []),
                                    "before": g.get("before", {}), "after": g.get("after", {})})
    return out


def telemetry(runs_dir: Path, rs: list[dict] | None = None) -> dict:
    spans = _jsonl(Path(runs_dir) / "traces.jsonl")
    by: dict[str, list[float]] = defaultdict(list)
    for s in spans:
        if s.get("duration_ms") is not None:
            by[s.get("name", "?")].append(float(s["duration_ms"]))
    nodes = [{"span": k, "count": len(v), "p50_ms": _pct(v, 50), "p95_ms": _pct(v, 95), "max_ms": round(max(v), 2)}
             for k, v in by.items()]
    nodes.sort(key=lambda x: -(x["p95_ms"] or 0))
    rs = runs(runs_dir) if rs is None else rs
    tokens = [r["usage"].get("total_tokens", 0) for r in rs if r["usage"]]
    cost = [r["usage"].get("est_cost_usd", 0.0) for r in rs if r["usage"]]
    return {"spans": len(spans), "by_span": nodes, "runs": len(rs),
            "tokens_total": sum(tokens), "tokens_mean": round(statistics.mean(tokens), 1) if tokens else None,
            "cost_total_usd": round(sum(cost), 6), "llm_calls": sum(r["usage"].get("llm_calls", 0) for r in rs if r["usage"]),
            "statuses": dict(Counter(r["status"] for r in rs))}


def budgets(orch, rs: list[dict]) -> dict:
    q = getattr(orch, "quota", None)
    if q is None:
        return {"enabled": False, "users": []}
    users = sorted({r["user"] for r in rs if r.get("user")})
    out = []
    for u in users:
        try:
            used, lim = q.used(u), q.limits(u).as_dict()
        except Exception as e:  # noqa: BLE001
            out.append({"user": u, "error": type(e).__name__})
            continue
        out.append({"user": u, "used": used, "limits": lim})
    return {"enabled": True, "day": q.today(), "tz": getattr(q, "tz_name", None), "users": out}


def ops(runs_dir: Path, orch=None, docs: Path = DOCS) -> dict:
    rs = runs(runs_dir)
    return {"budgets": budgets(orch, rs) if orch is not None else {"enabled": False, "users": []},
            "telemetry": telemetry(runs_dir, rs), "serving": serving(docs), "dspy": dspy()}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
