"""Tool permission policy (client-side; never trust server-provided hints alone).

MCP ToolAnnotations are *hints* from the server. The spec says clients must not
make trust decisions from them for untrusted servers, so the authoritative risk
tier lives here and is cross-checked against the server's annotations.
"""
from __future__ import annotations

from typing import Any

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


def evaluate(plan: list[dict]) -> dict:
    """Return {"allowed": bool, "needs_approval": bool, "denied_tools": [...], "approval_steps": [...]}"""
    denied, approval_steps = [], []
    for i, step in enumerate(plan):
        pol = TOOL_POLICY.get(step.get("tool", ""))
        if pol is None:
            denied.append(step.get("tool"))
        elif pol["approval"]:
            approval_steps.append(i)
    return {
        "allowed": not denied,
        "needs_approval": bool(approval_steps),
        "denied_tools": denied,
        "approval_steps": approval_steps,
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
