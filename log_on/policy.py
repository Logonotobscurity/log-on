"""Input policy (pre-planning guardrail). Deterministic, explainable rules.

Returns a decision dict: {"allowed": bool, "reasons": [...], "rule_ids": [...], "normalized": bool}

Phase 4 (red-team fixes): rules are matched against the raw goal AND a normalised copy, so obfuscated attacks hit
the same rules: Unicode NFKC + confusable (Cyrillic/Greek homoglyph) folding, zero-width/format characters
removed, leetspeak inside words decoded, intra-word hyphens/dots/asterisks removed, whitespace collapsed.
Base64 blobs are decoded and checked too, and "decode this and do what it says" is refused outright.
New rule families: jailbreak personas / "no rules" role-play, synonym and multilingual (French, Pidgin)
instruction overrides.
"""
from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern
    reason: str


_OVERRIDE_VERB = r"(ignore|disregard|forget|overlook|override|bypass|skip|drop|neglect|ignorez|oubliez|ignora|olvida)"
_PRIOR = r"(previous|prior|above|all|earlier|preceding|existing|di|toutes?|les|précédentes?|anteriores|your|the)"
_RULES = r"(instructions?|rules?|polic(y|ies)|guardrails?|safety|restrictions?|guidelines?|consignes|règles|instrucciones)"

RULES: list[Rule] = [
    Rule("prompt_injection",
         re.compile(r"\b" + _OVERRIDE_VERB + r"\b.{0,30}\b" + _PRIOR + r"\b.{0,20}\b" + _RULES, re.I),
         "Looks like a prompt-injection attempt to override instructions."),
    Rule("prompt_injection",
         re.compile(r"\b(system|developer|admin)\s*(prompt|message|note|notice|override)\s*:|^\s*```\s*(system|developer)\b"
                    r"|\byour (new|real) instructions are\b", re.I | re.M),
         "Looks like an injected system/developer message."),
    Rule("jailbreak",
         re.compile(r"\b(you are|you're|act as|pretend (to be|you are)|roleplay as|play a game where you are)\b.{0,40}"
                    r"\b(dan|unrestricted|unfiltered|uncensored|jailbroken|evil|no (rules|restrictions|limits|filters))\b"
                    r"|\b(developer|god|debug|sudo) mode\b|\bjailbreak\b|\bdo anything now\b"
                    r"|\b(safety|content|privacy) (filters?|rules?|guardrails?) (are |is )?(off|disabled|removed)\b"
                    r"|\bwith no (rules|restrictions|limits|filters)\b"
                    r"|\b(bypass|disable|turn off|switch off)\b.{0,20}\b(privacy|safety|security|content)\b.{0,10}"
                    r"\b(rules?|filters?|checks?|guard\w*)\b"
                    r"|\bpretend you are my (late )?grand(mother|ma|father|pa)\b", re.I),
         "Looks like a jailbreak (persona / role-play / 'no rules' framing)."),
    Rule("encoded_instructions",
         re.compile(r"\b(decode|decipher|base64|rot13|reverse)\b.{0,40}\b(do|follow|execute|obey|run|act on)\b"
                    r".{0,20}\b(it|this|what it says|the instructions?)\b", re.I),
         "Encoded or hidden instructions are refused."),
    Rule("bulk_pii_exfiltration",
         re.compile(r"\b(export|dump|download|list|send|leak|print|show|give)\b.{0,40}\b(all|every|entire|bulk)\b.{0,40}"
                    r"\b(customers?|users?|records?|card numbers?|nins?|passwords?|emails?|phone numbers?)", re.I),
         "Bulk export of personal data is not permitted."),
    Rule("credential_access",
         re.compile(r"\b(password|api[ _-]?key|secret|private key|access token|otp|pin)s?\b.{0,30}\b(reveal|show|give|send|steal|get|extract)|\b(reveal|show|give|send|steal|get|extract)\b.{0,30}\b(password|api[ _-]?key|secret|private key|access token|otp)s?\b"
                    r"|\bwhat(?:'s| is| are)\b.{0,25}\b(password|api[ _-]?key|private key|access token|otp)s?\s+(for|of|on|to)\b", re.I),
         "Requests for credentials or secrets are refused."),
    Rule("harmful",
         re.compile(r"\b(make|build|synthesi[sz]e)\b.{0,30}\b(bomb|explosive|malware|ransomware|nerve agent)\b", re.I),
         "Harmful content is refused."),
    Rule("fraud",
         re.compile(r"\b(fake|forge|spoof)\b.{0,30}\b(bvn|nin|id card|invoice|bank statement|identity)\b", re.I),
         "Identity/document fraud is refused."),
]

MAX_GOAL_CHARS = 2000

# confusables: the common Cyrillic / Greek look-alikes of Latin letters
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "һ": "h",
    "А": "A", "Е": "E", "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X", "І": "I", "К": "K", "М": "M", "Н": "H",
    "В": "B", "α": "a", "ο": "o", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ρ": "p", "τ": "t", "υ": "u",
})
_LEET = str.maketrans({"4": "a", "@": "a", "3": "e", "1": "i", "!": "i", "0": "o", "5": "s", "$": "s", "7": "t"})
_LEET_WORD = re.compile(r"[A-Za-z@$!]*[0-9@$!][A-Za-z0-9@$!]*")
_B64 = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")


def _deleet(m: re.Match) -> str:
    w = m.group(0)
    letters = sum(c.isalpha() for c in w)
    # only words that are mostly letters with a few digit substitutions (keeps C-1001, Q3, 2^10 intact)
    if letters >= 2 and letters >= len(w) - letters and not re.fullmatch(r"[A-Za-z]\d+|\d+[A-Za-z]?", w):
        return w.translate(_LEET)
    return w


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Cf")  # zero-width & other format chars
    t = t.translate(_CONFUSABLES)
    t = _LEET_WORD.sub(_deleet, t)
    t = re.sub(r"(?<=[A-Za-z])[-_.*·](?=[A-Za-z])", "", t)  # ig-nore / ig.nore / ig*nore
    t = re.sub(r"\b(?:[A-Za-z] ){3,}[A-Za-z]\b", lambda m: m.group(0).replace(" ", ""), t)  # i g n o r e
    return re.sub(r"\s+", " ", t).strip()


def _decoded_payloads(text: str) -> list[str]:
    out = []
    for blob in _B64.findall(text):
        try:
            raw = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True)
            s = raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if sum(ch.isprintable() for ch in s) >= 0.9 * len(s) and re.search(r"[A-Za-z]{3,} [A-Za-z]{3,}", s):
            out.append(s)
    return out


def check_input(goal: str) -> dict:
    reasons, ids = [], []
    if not goal or not goal.strip():
        reasons.append("Empty goal."); ids.append("empty")
    if len(goal) > MAX_GOAL_CHARS:
        reasons.append(f"Goal longer than {MAX_GOAL_CHARS} characters."); ids.append("too_long")
    norm = normalize(goal or "")
    views = [goal or "", norm] + [normalize(p) for p in _decoded_payloads(goal or "")]
    for rule in RULES:
        if rule.id in ids:
            continue
        if any(rule.pattern.search(v) for v in views):
            reasons.append(rule.reason); ids.append(rule.id)
    hit_raw = any(r.pattern.search(goal or "") for r in RULES)
    return {"allowed": not reasons, "reasons": reasons, "rule_ids": ids,
            "normalized": bool(ids) and not hit_raw and norm != (goal or "").strip()}
