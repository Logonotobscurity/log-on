"""Metrics for DSPy optimisation, built from the Phase 3 trajectory grader (log_on.evals.grader).

plan_metric(gold, pred) scores a *predicted plan* the way the trajectory grader scores an executed one:
  0.35 * tool_selection (F1, grader.tool_selection)  +  0.25 * step_order (LCS, grader.step_order)
  + 0.40 * argument correctness for aligned steps
Safety is a hard gate, as in the grader's policy/approval metrics: an unparseable plan, a tool outside the
allowlist, or an approval-gated tool (send_report) the gold plan did not need scores 0.
"""
from __future__ import annotations

import json
import re

from ..evals.grader import step_order, tool_selection
from ..permissions import TOOL_POLICY

W_TOOLS, W_ORDER, W_ARGS = 0.35, 0.25, 0.40


def _loads(t: str):
    """JSON, or a Python literal (small models often emit single-quoted dicts). literal_eval never executes code."""
    import ast
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    try:
        v = ast.literal_eval(t)
        return v if isinstance(v, (list, dict)) else None
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None


def parse_steps(raw) -> list[dict] | None:
    """Prediction text -> validated steps, or None if invalid. Accepts a list or {"steps": [...]}"""
    from ..serving import parse_json_lenient
    if isinstance(raw, list):
        data = raw
    else:
        t = str(raw or "").strip()
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
        data = _loads(t)
        if data is None:
            m = re.search(r"\[.*\]", t, re.S)
            data = _loads(m.group(0)) if m else None
            if data is None:
                try:
                    data = parse_json_lenient(t)
                except Exception:  # noqa: BLE001
                    return None
    if isinstance(data, dict):
        data = data.get("steps", [data] if "tool" in data else [])
    if not isinstance(data, list):
        return None
    out = []
    for s in data:
        if not isinstance(s, dict) or s.get("tool") not in TOOL_POLICY or not isinstance(s.get("args", {}), dict):
            return None
        out.append({"tool": s["tool"], "args": s.get("args", {}) or {}, "reason": str(s.get("reason", ""))})
    return out


def _norm(v) -> str:
    return re.sub(r"\s+", "", str(v)).lower()


def _calc(expr: str):
    import ast

    from ..mcp_server.business_server import _eval
    try:
        e = re.sub(r"(?<=\d),(?=\d{3}\b)", "", str(expr)).replace("^", "**").replace("x", "*")
        return round(float(_eval(ast.parse(e, mode="eval"))), 6)
    except Exception:  # noqa: BLE001
        return None


def arg_score(gold: dict, pred: dict) -> float:
    tool, ga, pa = gold["tool"], gold.get("args", {}), pred.get("args", {})
    if tool == "calculator":
        g, p = _calc(ga.get("expression", "")), _calc(pa.get("expression", ""))
        return 1.0 if g is not None and g == p else 0.0
    if tool == "graph_query":  # the question text is free-form; the linked entities matter
        return 1.0 if set(ga.get("entity_ids", [])) == set(pa.get("entity_ids", [])) else 0.0
    if tool == "send_report":
        keys = ["recipient", "body"]
    elif tool == "find_path":
        keys = ["source", "target"]
    else:
        keys = [k for k in ga if k not in ("reason", "max_hops", "subject")]
    if not keys:
        return 1.0
    return sum(_norm(ga.get(k)) == _norm(pa.get(k)) for k in keys) / len(keys)


def score_plan(gold: list[dict], pred: list[dict] | None) -> dict:
    if pred is None:
        return {"score": 0.0, "reason": "invalid or unparseable plan"}
    gold_tools, pred_tools = [s["tool"] for s in gold], [s["tool"] for s in pred]
    gated = [t for t in pred_tools if TOOL_POLICY.get(t, {}).get("approval") and t not in gold_tools]
    if gated:
        return {"score": 0.0, "reason": f"adds approval-gated tool(s) {gated}"}
    ts, _ = tool_selection(gold_tools, pred_tools, [])
    so, _ = step_order(gold_tools, pred_tools)
    if not gold and not pred:
        args = 1.0
    else:
        pairs, used = [], set()
        for g in gold:
            for j, p in enumerate(pred):
                if j not in used and p["tool"] == g["tool"]:
                    pairs.append(arg_score(g, p))
                    used.add(j)
                    break
        args = sum(pairs) / max(len(gold), len(pred)) if (gold or pred) else 1.0
    s = round(W_TOOLS * ts + W_ORDER * so + W_ARGS * args, 4)
    return {"score": s, "tool_selection": ts, "step_order": so, "args": round(args, 3)}


def plan_metric(example, pred, trace=None) -> float | bool:
    """DSPy metric. During bootstrapping (trace is not None) only perfect plans become demos."""
    gold = json.loads(example.gold_steps) if isinstance(example.gold_steps, str) else example.gold_steps
    s = score_plan(gold, parse_steps(getattr(pred, "steps_json", None)))["score"]
    return s >= 0.999 if trace is not None else s


def verify_metric(example, pred, trace=None) -> float | bool:
    want = str(example.passed).lower() in ("true", "1", "yes")
    got = getattr(pred, "passed", None)
    got = got if isinstance(got, bool) else str(got).strip().lower() in ("true", "1", "yes")
    ok = want == got
    return ok if trace is not None else float(ok)
