"""Optional backend that runs the same validators through guardrails-ai's Guard.

Installed without a Guardrails Hub token: we register our own validators with
`@register_validator`, so nothing is downloaded from the Hub. Telemetry is off.
"""
from __future__ import annotations

from typing import Any

from .base import Guard as NativeGuard, GuardOutcome, OnFail, Validator

try:  # pragma: no cover - import guard
    from guardrails import Guard as GRGuard, OnFailAction
    from guardrails.validators import FailResult as GRFail, PassResult as GRPass, Validator as GRValidator, register_validator
    AVAILABLE = True
except Exception:  # noqa: BLE001
    AVAILABLE = False

_ONFAIL_MAP = {}
if AVAILABLE:
    _ONFAIL_MAP = {
        OnFail.FIX: OnFailAction.FIX,
        OnFail.EXCEPTION: OnFailAction.EXCEPTION,
        OnFail.REFRAIN: OnFailAction.REFRAIN,
        OnFail.NOOP: OnFailAction.NOOP,
    }
    _REGISTERED: dict[str, type] = {}

    def _wrap(inner: Validator):
        """Create (once per name) a guardrails Validator class delegating to our validator."""
        key = f"log_on/{inner.name}"
        if key not in _REGISTERED:
            @register_validator(name=key, data_type="string")
            class _Adapter(GRValidator):  # type: ignore[misc, valid-type]
                def __init__(self, delegate=None, **kw):
                    super().__init__(**kw)
                    self._delegate = delegate

                def _validate(self, value: Any, metadata: dict) -> Any:
                    r = self._delegate.validate(str(value), metadata)
                    if r.passed:
                        return GRPass()
                    return GRFail(error_message=r.error_message, fix_value=r.fix_value)
            _REGISTERED[key] = _Adapter
        return _REGISTERED[key](delegate=inner, on_fail=_ONFAIL_MAP[inner.on_fail])


class GuardrailsGuard(NativeGuard):
    backend = "guardrails-ai"

    def __init__(self, validators: list[Validator]):
        if not AVAILABLE:
            raise RuntimeError("guardrails-ai is not installed")
        super().__init__(validators)

    def validate(self, value: str, metadata: dict[str, Any] | None = None) -> GuardOutcome:
        # Run each validator as its own guardrails Guard so we can attribute violations
        # exactly like the native backend (Guardrails' own history API also exposes this).
        current = value
        violations: list[dict[str, Any]] = []
        for v in self.validators:
            g = GRGuard()
            g.configure(allow_metrics_collection=False)
            g.use(_wrap(v))
            try:
                res = g.validate(current, metadata=metadata or {})
            except Exception as exc:  # guardrails raises ValidationError on EXCEPTION
                violations.append({"validator": v.name, "message": str(exc), "on_fail": v.on_fail.value})
                return GuardOutcome(raw=value, output=None, passed=False, blocked=True, violations=violations)
            out = res.validated_output
            if out != current or (not res.validation_passed):
                violations.append({"validator": v.name, "message": "fixed by guardrails", "on_fail": v.on_fail.value})
            if out is None:
                return GuardOutcome(raw=value, output=None, passed=False, violations=violations)
            current = out
        return GuardOutcome(raw=value, output=current, passed=not violations, violations=violations)
