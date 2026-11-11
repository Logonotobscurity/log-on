"""Local MCP server (official `mcp` Python SDK v2, stdio transport) exposing mock business tools.

Run standalone:  python -m log_on.mcp_server.business_server
(In mcp v1 this was `FastMCP`; v2 renamed it to `MCPServer`.)
"""
from __future__ import annotations

import ast
import json
import operator
import os
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

server = MCPServer("log_on-business-tools", instructions="Mock CRM + reporting tools for the LOG_ON safe orchestrator.")

# Mock CRM. Deliberately contains PII so the output guardrails have something to catch.
CUSTOMERS = {
    "C-1001": {"name": "Adaeze Okafor", "email": "adaeze.okafor@example.com", "phone": "08031234567",
               "nin": "12345678901", "card": "4111 1111 1111 1111", "tier": "gold", "balance_ngn": 182500,
               "notes": "Prefers WhatsApp updates."},
    "C-1002": {"name": "Tunde Bakare", "email": "tunde@example.org", "phone": "+234 802 555 0199",
               "nin": "98765432109", "card": "5500-0000-0000-0004", "tier": "silver", "balance_ngn": 40250,
               "notes": "Asked for a statement in October."},
    "C-1666": {"name": "Test Account", "email": "qa@example.com", "phone": "07000000000",
               "nin": "11122233344", "card": "", "tier": "internal", "balance_ngn": 0,
               "notes": "temp admin password: hunter2-please-rotate"},
}

REPORTS = {
    "q3 revenue": "Q3 revenue: NGN 48.2m (+12% QoQ). Top segment: SME lending. Churn 3.1%.",
    "weekly ops": "Weekly ops: 1,204 tickets closed, median response 14 min, 2 incidents (resolved).",
}


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def lookup_customer(customer_id: str) -> str:
    """Look up a customer record in the CRM by id (e.g. C-1001). Returns JSON."""
    rec = CUSTOMERS.get(customer_id.upper())
    if rec is None:
        return json.dumps({"error": f"customer {customer_id} not found"})
    return json.dumps({"customer_id": customer_id.upper(), **rec})


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def get_report(name: str) -> str:
    """Fetch an internal report summary by name (e.g. 'Q3 revenue', 'weekly ops')."""
    return REPORTS.get(name.lower().strip(), f"No report named '{name}'.")


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True))
def send_report(recipient: str, subject: str, body: str) -> str:
    """Send a report to a recipient. Mock: writes to the local outbox file instead of emailing."""
    outbox = Path(os.environ.get("LOG_ON_OUTBOX", "outbox.jsonl"))
    outbox.parent.mkdir(parents=True, exist_ok=True)
    msg = {"ts": datetime.now(timezone.utc).isoformat(), "to": recipient, "subject": subject, "body": body}
    with outbox.open("a", encoding="utf-8") as f:
        f.write(json.dumps(msg) + "\n")
    return f"Report '{subject}' queued for {recipient} (mock outbox: {outbox.name})."


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
        ast.USub: operator.neg, ast.UAdd: operator.pos}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        if isinstance(node.op, ast.Pow) and abs(_eval(node.right)) > 100:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("unsupported expression")


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def calculator(expression: str) -> str:
    """Safely evaluate an arithmetic expression (+ - * / ** % //, parentheses). No eval()."""
    try:
        val = _eval(ast.parse(expression, mode="eval"))
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"
    return str(round(val, 10) if isinstance(val, float) else val)


if __name__ == "__main__":
    server.run("stdio")
