"""Pluggable validator interface mirroring Guardrails AI's design.

Guardrails pattern:            this module:
  Validator._validate()   ->     Validator.validate(value, metadata) -> PassResult | FailResult
  FailResult.fix_value    ->     FailResult.fix_value
  on_fail=OnFailAction.*  ->     Validator(on_fail=OnFail.FIX | EXCEPTION | NOOP | REFRAIN)
  Guard().use(...).validate(text) -> Guard([...]).validate(text) -> GuardOutcome
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class OnFail(str, Enum):
    FIX = "fix"            # replace value with fix_value (e.g. redacted text) and continue
    EXCEPTION = "exception"  # stop: the output is blocked
    REFRAIN = "refrain"    # return nothing (empty output) but do not raise
    NOOP = "noop"          # record the violation only


@dataclass
class PassResult:
    passed: bool = True


@dataclass
class FailResult:
    error_message: str
    fix_value: str | None = None
    passed: bool = False


class GuardBlocked(Exception):
    def __init__(self, validator: str, message: str):
        super().__init__(f"{validator}: {message}")
        self.validator = validator
        self.message = message


class Validator(ABC):
    name: str = "validator"

    def __init__(self, on_fail: OnFail = OnFail.FIX):
        self.on_fail = OnFail(on_fail)

    @abstractmethod
    def validate(self, value: str, metadata: dict[str, Any] | None = None) -> PassResult | FailResult: ...


@dataclass
class GuardOutcome:
    raw: str
    output: str | None
    passed: bool
    blocked: bool = False
    violations: list[dict[str, Any]] = field(default_factory=list)


class Guard:
    """Runs validators in order; FIX chains the fixed value into the next validator."""

    backend = "native"

    def __init__(self, validators: list[Validator]):
        self.validators = validators

    def validate(self, value: str, metadata: dict[str, Any] | None = None) -> GuardOutcome:
        current: str | None = value
        violations: list[dict[str, Any]] = []
        for v in self.validators:
            res = v.validate(current or "", metadata or {})
            if res.passed:
                continue
            violations.append({"validator": v.name, "message": res.error_message, "on_fail": v.on_fail.value})
            if v.on_fail is OnFail.FIX and res.fix_value is not None:
                current = res.fix_value
            elif v.on_fail is OnFail.EXCEPTION:
                return GuardOutcome(raw=value, output=None, passed=False, blocked=True, violations=violations)
            elif v.on_fail is OnFail.REFRAIN:
                return GuardOutcome(raw=value, output=None, passed=False, violations=violations)
        return GuardOutcome(raw=value, output=current, passed=not violations, violations=violations)
