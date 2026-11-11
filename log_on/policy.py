"""Input policy (pre-planning guardrail). Deterministic, explainable rules.

Returns a decision dict: {"allowed": bool, "reasons": [...], "rule_ids": [...]}
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern
    reason: str


RULES: list[Rule] = [
    Rule("prompt_injection",
         re.compile(r"\b(ignore|disregard|forget)\b.{0,30}\b(previous|prior|above|all)\b.{0,20}\b(instructions?|rules?|polic(y|ies))", re.I),
         "Looks like a prompt-injection attempt to override instructions."),
    Rule("bulk_pii_exfiltration",
         re.compile(r"\b(export|dump|download|list|send|leak)\b.{0,40}\b(all|every|entire|bulk)\b.{0,40}\b(customers?|users?|records?|card numbers?|nins?|passwords?|emails?)", re.I),
         "Bulk export of personal data is not permitted."),
    Rule("credential_access",
         re.compile(r"\b(password|api[ _-]?key|secret|private key|access token|otp|pin)s?\b.{0,30}\b(reveal|show|give|send|steal|get|extract)|\b(reveal|show|give|send|steal|get|extract)\b.{0,30}\b(password|api[ _-]?key|secret|private key|access token|otp)s?\b", re.I),
         "Requests for credentials or secrets are refused."),
    Rule("harmful",
         re.compile(r"\b(make|build|synthesi[sz]e)\b.{0,30}\b(bomb|explosive|malware|ransomware|nerve agent)\b", re.I),
         "Harmful content is refused."),
    Rule("fraud",
         re.compile(r"\b(fake|forge|spoof)\b.{0,30}\b(bvn|nin|id card|invoice|bank statement|identity)\b", re.I),
         "Identity/document fraud is refused."),
]

MAX_GOAL_CHARS = 2000


def check_input(goal: str) -> dict:
    reasons, ids = [], []
    if not goal or not goal.strip():
        reasons.append("Empty goal."); ids.append("empty")
    if len(goal) > MAX_GOAL_CHARS:
        reasons.append(f"Goal longer than {MAX_GOAL_CHARS} characters."); ids.append("too_long")
    for rule in RULES:
        if rule.pattern.search(goal or ""):
            reasons.append(rule.reason); ids.append(rule.id)
    return {"allowed": not reasons, "reasons": reasons, "rule_ids": ids}
