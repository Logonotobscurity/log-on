"""Output guardrails. `make_guard()` picks the backend via LOG_ON_GUARD_BACKEND=native|guardrails|auto."""
from __future__ import annotations

import os

from .base import FailResult, Guard, GuardBlocked, GuardOutcome, OnFail, PassResult, Validator
from .validators import default_validators, redact_text


def make_guard(backend: str | None = None) -> Guard:
    backend = (backend or os.getenv("LOG_ON_GUARD_BACKEND", "native")).lower()
    if backend in ("guardrails", "auto"):
        from .guardrails_backend import AVAILABLE, GuardrailsGuard
        if AVAILABLE:
            return GuardrailsGuard(default_validators())
        if backend == "guardrails":
            raise RuntimeError("LOG_ON_GUARD_BACKEND=guardrails but guardrails-ai is not importable")
    return Guard(default_validators())


__all__ = ["FailResult", "Guard", "GuardBlocked", "GuardOutcome", "OnFail", "PassResult", "Validator",
           "default_validators", "make_guard", "redact_text"]
