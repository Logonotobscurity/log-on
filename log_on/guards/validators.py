"""Concrete validators: PII redaction (email, phone, Nigerian NIN, payment card),
secret-leak and toxicity / blocked-topic checks. Pure regex + wordlists, no network."""
from __future__ import annotations

import re
from typing import Any

from .base import FailResult, OnFail, PassResult, Validator

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Nigerian NIN: exactly 11 digits, usually labelled. Unlabelled 11-digit runs that are not phone numbers also caught.
NIN_LABELLED_RE = re.compile(r"(?i)\b(NIN|national identification number)\b[\s:#-]*(\d{11})\b")
NIN_BARE_RE = re.compile(r"(?<![\d+])(?!0[789][01]\d{8}\b)\d{11}(?!\d)")
# Phones: Nigerian local 0[7-9][0-1]XXXXXXXX, +234..., and generic international +CC...
PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?234[ -]?[789][01]\d(?:[ -]?\d){7}|0[789][01]\d(?:[ -]?\d){7}|\+\d{1,3}[ -]?\(?\d{2,4}\)?(?:[ -]?\d){6,10})(?!\d)"
)


def luhn_ok(digits: str) -> bool:
    d = [int(c) for c in digits][::-1]
    total = sum(d[0::2]) + sum(sum(divmod(2 * x, 10)) for x in d[1::2])
    return total % 10 == 0


class _Redactor(Validator):
    label = "PII"

    def find(self, value: str) -> list[tuple[int, int]]:
        raise NotImplementedError

    def validate(self, value: str, metadata: dict[str, Any] | None = None):
        spans = self.find(value)
        if not spans:
            return PassResult()
        out, last = [], 0
        for s, e in spans:
            out.append(value[last:s]); out.append(f"<{self.label}>"); last = e
        out.append(value[last:])
        return FailResult(error_message=f"{len(spans)} {self.label} value(s) found", fix_value="".join(out))


class EmailRedactor(_Redactor):
    name, label = "email_pii", "EMAIL"

    def find(self, v):
        return [m.span() for m in EMAIL_RE.finditer(v)]


class CardRedactor(_Redactor):
    name, label = "card_pii", "CARD"

    def find(self, v):
        # Consider every run of digit groups joined by single spaces/dashes, and every contiguous
        # window of groups inside it, so an adjacent number (e.g. a NIN) cannot hide a card.
        spans = []
        for run in re.finditer(r"\d+(?:[ -]\d+)*", v):
            groups = [(m.start() + run.start(), m.end() + run.start()) for m in re.finditer(r"\d+", run.group())]
            i = 0
            while i < len(groups):
                hit = None
                for j in range(len(groups) - 1, i - 1, -1):
                    window = groups[i:j + 1]
                    lens = [e - s for s, e in window]
                    # Card-shaped: one unbroken 13-19 digit block, or groups of 4-6 (4-4-4-4, 4-6-5 ...).
                    shaped = len(window) == 1 or all(4 <= n <= 6 for n in lens)
                    digits = re.sub(r"\D", "", v[groups[i][0]:groups[j][1]])
                    if shaped and 13 <= len(digits) <= 19 and luhn_ok(digits):
                        hit = j
                        break
                if hit is None:
                    i += 1
                else:
                    spans.append((groups[i][0], groups[hit][1]))
                    i = hit + 1
        return spans


class NINRedactor(_Redactor):
    name, label = "nin_pii", "NIN"

    def find(self, v):
        spans = [m.span(2) for m in NIN_LABELLED_RE.finditer(v)]
        spans += [m.span() for m in NIN_BARE_RE.finditer(v)]
        return sorted(set(spans))


class PhoneRedactor(_Redactor):
    name, label = "phone_pii", "PHONE"

    def find(self, v):
        return [m.span() for m in PHONE_RE.finditer(v)]


SECRET_RE = re.compile(
    r"(?i)\b(sk-(live|test|proj)?-?[A-Za-z0-9]{12,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|password\s*[:=]\s*\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


class SecretLeak(Validator):
    name = "secret_leak"

    def __init__(self, on_fail: OnFail = OnFail.EXCEPTION):
        super().__init__(on_fail)

    def validate(self, value, metadata=None):
        m = SECRET_RE.search(value)
        if m:
            return FailResult(error_message="credential/secret detected in output",
                              fix_value=SECRET_RE.sub("<SECRET>", value))
        return PassResult()


class BlockedTopics(Validator):
    """Keyword-based toxicity / blocked-topic check. Swap for a classifier (e.g. Guardrails Hub ToxicLanguage) later."""
    name = "blocked_topics"
    DEFAULT_TERMS = {
        "toxicity": ["idiot", "stupid", "moron", "worthless", "shut up", "kill yourself"],
        "violence": ["bomb", "explosive", "weapon", "attack plan"],
        "gambling_advice": ["guaranteed win", "sure bet", "fixed match"],
    }

    def __init__(self, terms: dict[str, list[str]] | None = None, on_fail: OnFail = OnFail.EXCEPTION):
        super().__init__(on_fail)
        self.terms = terms or self.DEFAULT_TERMS
        self._re = {t: re.compile(r"(?i)\b(" + "|".join(map(re.escape, ws)) + r")\b") for t, ws in self.terms.items()}

    def validate(self, value, metadata=None):
        hits = sorted({t for t, rx in self._re.items() if rx.search(value)})
        if hits:
            return FailResult(error_message=f"blocked topic(s): {', '.join(hits)}")
        return PassResult()


def default_validators() -> list[Validator]:
    # Order matters: secrets/toxicity first (block), then card before NIN/phone so 16-digit cards are not split.
    return [SecretLeak(), BlockedTopics(), EmailRedactor(), CardRedactor(), NINRedactor(), PhoneRedactor()]


def redact_text(text: str) -> str:
    """Best-effort PII + secret scrub used for audit logging (never raises)."""
    for v in [EmailRedactor(), CardRedactor(), NINRedactor(), PhoneRedactor()]:
        r = v.validate(text)
        if not r.passed and r.fix_value is not None:
            text = r.fix_value
    return SECRET_RE.sub("<SECRET>", text)
