"""Tool permission policy (client-side; never trust server-provided hints alone).

MCP ToolAnnotations are *hints* from the server. The spec says clients must not
make trust decisions from them for untrusted servers, so the authoritative risk
tier lives here and is cross-checked against the server's annotations.
"""
from __future__ import annotations

import os
import re
import unicodedata
from typing import Any

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# tool -> risk tier. low = auto-run, medium = auto-run but output must pass PII guard,
# high = externally visible side effect -> human approval required.
TOOL_POLICY: dict[str, dict[str, Any]] = {
    "calculator": {"risk": "low", "approval": False},
    "get_report": {"risk": "low", "approval": False},
    "lookup_customer": {"risk": "medium", "approval": False},
    "send_report": {"risk": "high", "approval": True},
    # Phase 2: read-only knowledge-graph tools. medium = output still passes the PII guard.
    "graph_query": {"risk": "medium", "approval": False},
    "find_path": {"risk": "low", "approval": False},
    "entity_lookup": {"risk": "medium", "approval": False},
}


# Phase 4 (red-team fix): tools that send data out, and the argument naming where it goes. The destination must be
# one the *user* typed in this request; a planner (or a poisoned document / memory) cannot choose it.
EGRESS_ARGS: dict[str, str] = {"send_report": "recipient"}


def _norm_addr(a: str) -> str:
    return unicodedata.normalize("NFKC", str(a or "")).strip().strip("<>.,;").lower()


def recipient_provenance(plan: list[dict], goal: str) -> list[dict]:
    """Egress steps whose destination does not appear verbatim in the user's goal."""
    stated = {_norm_addr(m) for m in EMAIL_RE.findall(unicodedata.normalize("NFKC", goal or ""))}
    bad = []
    for i, step in enumerate(plan):
        arg = EGRESS_ARGS.get(step.get("tool", ""))
        if not arg:
            continue
        dest = _norm_addr((step.get("args") or {}).get(arg, ""))
        if dest not in stated:
            bad.append({"step": i, "tool": step["tool"], "destination": dest or "(none)"})
    return bad


# Phase 4 (red-team fix, found with the real local model): personal-data records may only leave via egress tools to
# the organisation's own domains, even when the user typed the address and a human would approve.
PII_SOURCE_TOOLS = {"lookup_customer", "entity_lookup"}
STEP_REF_RE = re.compile(r"\{\{\s*steps\[(\d+)\]\.output\s*\}\}")


def trusted_domains() -> set[str]:
    raw = os.getenv("LOG_ON_TRUSTED_DOMAINS", "acme.example")
    return {d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()}


def pii_dataflow(plan: list[dict]) -> list[dict]:
    """Egress steps that would carry the output of a personal-data tool to an untrusted domain."""
    trusted, bad = trusted_domains(), []
    for i, step in enumerate(plan):
        arg = EGRESS_ARGS.get(step.get("tool", ""))
        if not arg:
            continue
        args = step.get("args") or {}
        refs = {int(n) for v in args.values() for n in STEP_REF_RE.findall(str(v))}
        sources = [plan[n]["tool"] for n in refs if n < len(plan) and plan[n].get("tool") in PII_SOURCE_TOOLS]
        dom = _norm_addr(args.get(arg, "")).rpartition("@")[2]
        if sources and not any(dom == t or dom.endswith("." + t) for t in trusted):
            bad.append({"step": i, "tool": step["tool"], "sources": sources, "domain": dom or "(none)"})
    return bad


def invalid_refs(plan: list[dict]) -> list[dict]:
    """Phase 4 (red-team fix, found with the local model): steps whose `{{steps[n].output}}` points at itself, a later
    step or a missing step. Such a plan is malformed; refusing it up front means a human is never asked to approve a
    send whose body cannot be built (the 1.5B model planned `send_report(body={{steps[0].output}})` as step 0)."""
    bad = []
    for i, step in enumerate(plan):
        refs = {int(n) for v in (step.get("args") or {}).values() for n in STEP_REF_RE.findall(str(v))}
        wrong = sorted(n for n in refs if n >= i)
        if wrong:
            bad.append({"step": i, "tool": step.get("tool"), "refs": wrong})
    return bad


def evaluate(plan: list[dict], goal: str | None = None) -> dict:
    """Return {"allowed", "needs_approval", "denied_tools", "approval_steps", "provenance_violations"}.

    With `goal` given, egress destinations are checked against it (recipient provenance)."""
    denied, approval_steps = [], []
    for i, step in enumerate(plan):
        pol = TOOL_POLICY.get(step.get("tool", ""))
        if pol is None:
            denied.append(step.get("tool"))
        elif pol["approval"]:
            approval_steps.append(i)
    prov = recipient_provenance(plan, goal) if goal is not None else []
    flow = pii_dataflow(plan) if goal is not None else []
    refs = invalid_refs(plan)
    return {
        "allowed": not denied and not prov and not flow and not refs,
        "invalid_refs": refs,
        "dataflow_violations": flow,
        "needs_approval": bool(approval_steps),
        "denied_tools": denied,
        "approval_steps": approval_steps,
        "provenance_violations": prov,
    }


def annotation_mismatches(server_tools: dict[str, Any]) -> list[str]:
    """Flag tools the server says are read-only but our policy treats as high risk, or vice versa."""
    out = []
    for name, ann in server_tools.items():
        pol = TOOL_POLICY.get(name)
        if not pol or ann is None:
            continue
        ro = getattr(ann, "read_only_hint", None)
        if ro is True and pol["risk"] == "high":
            out.append(f"{name}: server claims read-only, local policy says high risk")
        if ro is False and pol["risk"] == "low":
            out.append(f"{name}: server says side effects, local policy says low risk")
    return out
