"""Shared "this tool result is a failure" signals (used by the MCP client's fail-fast and the Phase 3 verifier)."""
import re

FAIL_SIGNALS = re.compile(
    r"^\s*error:|^\s*\{\s*\"error\"|\bnot offered by server\b|\bNo report named\b|\bNo entity matches\b|"
    r"\bcouldn'?t map\b|\bNo path within\b|\bunknown-recipient\b|\bnot found\b|^skipped: depends on", re.I | re.M)


def failed(result: dict) -> bool:
    return (not result.get("ok", True)) or bool(FAIL_SIGNALS.search(str(result.get("output") or "")))
