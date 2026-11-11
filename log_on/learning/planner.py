"""Runtime adapters: a DSPy program behind LOG_ON's Planner / Verifier interfaces.

DSPyPlanner loads the *approved* program from the prompt registry (sha256-checked), calls the model through the
serving layer's endpoint, parses with the same allowlist as LLMPlanner and falls back to the rule planner on any
error. The planner name carries the prompt version (e.g. `dspy:planner@v1-20261111-ab12cd34`), so every audit
record and trace says which prompt produced a plan (roadmap O-3).
"""
from __future__ import annotations

import json
import threading

from ..planner import RuleBasedPlanner
from .metrics import parse_steps
from .registry import PromotionRefused, PromptRegistry
from .signatures import TOOLS_DOC, PlannerProgram, VerifierProgram, context_str, to_dspy_lm

_LOCK = threading.Lock()  # dspy.context is thread-local, but keep program calls serialised per process


class DSPyPlanner:
    def __init__(self, program: PlannerProgram | None = None, lm=None, provider=None, version: str = "unoptimized",
                 fallback=None):
        self.program = program or PlannerProgram()
        self.lm = lm if lm is not None else (to_dspy_lm(provider) if provider is not None else None)
        self.version = version
        self.name = f"dspy:planner@{version}"
        self.fallback = fallback or RuleBasedPlanner()
        self.last_error: str | None = None
        self.last_usage: dict | None = None
        self.model = getattr(self.lm, "model", "dspy")
        # self-hosted providers (llamacpp/vllm/...) are $0 per token in the Phase 3 cost ledger
        self.provider_name = getattr(provider, "name", None) or "dspy"

    @classmethod
    def from_registry(cls, provider=None, lm=None, registry: PromptRegistry | None = None) -> "DSPyPlanner":
        reg = registry or PromptRegistry()
        program, version = PlannerProgram(), "unoptimized"
        try:
            found = reg.load_active("planner")
            if found:
                version, path = found
                program.load(str(path))
        except Exception as exc:  # noqa: BLE001 - integrity failure or unreadable program -> rules, never a crash
            p = cls(PlannerProgram(), lm=lm, provider=provider, version="refused")
            p.fallback_only = f"{type(exc).__name__}: {exc}" if not isinstance(exc, PromotionRefused) else str(exc)
            return p
        return cls(program, lm=lm, provider=provider, version=version)

    def plan(self, goal: str, context: dict | None = None) -> list[dict]:
        import dspy
        self.last_error, self.last_usage = None, None
        if getattr(self, "fallback_only", None):
            self.last_error = self.fallback_only
            return self.fallback.plan(goal, context)
        try:
            with _LOCK, dspy.context(lm=self.lm):
                pred = self.program(goal=goal, context=context_str(context), tools=TOOLS_DOC)
            steps = parse_steps(pred.steps_json)
            if steps is None:
                raise ValueError(f"invalid plan from model: {str(pred.steps_json)[:120]!r}")
            self.last_usage = _usage_of(self.lm, self.provider_name)
            return steps
        except Exception as exc:  # noqa: BLE001 - any model/parse failure -> deterministic rules
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            return self.fallback.plan(goal, context)


def _usage_of(lm, provider: str = "dspy") -> dict | None:
    try:
        h = lm.history[-1]
        u = h.get("usage") or {}
        return {"model": getattr(lm, "model", "dspy"), "provider": provider, "calls": 1,
                "prompt_tokens": int(u.get("prompt_tokens") or 0), "completion_tokens": int(u.get("completion_tokens") or 0)}
    except Exception:  # noqa: BLE001
        return None


class DSPyVerifier:
    """Rules first (authoritative, as in Phase 3); the DSPy program only adds a model-graded check on top."""

    def __init__(self, program: VerifierProgram | None = None, lm=None, version: str = "unoptimized"):
        from ..loop.verifier import RuleVerifier
        self.rules = RuleVerifier()
        self.program = program or VerifierProgram()
        self.lm = lm
        self.version = version
        self.name = f"dspy:verifier@{version}+rules"
        self.last_usage, self.last_error = None, None

    def verify(self, goal, plan, tool_results, validated):
        import dspy
        verdict = self.rules.verify(goal, plan, tool_results, validated)
        self.last_usage, self.last_error = None, None
        if not verdict["passed"]:
            return {**verdict, "verifier": "rule-based (dspy not consulted: hard checks failed)"}
        try:
            with _LOCK, dspy.context(lm=self.lm):
                pred = self.program(goal=goal, plan_json=json.dumps(plan, default=str)[:3000],
                                    results_json=json.dumps(validated, default=str)[:3000])
            ok = pred.passed if isinstance(pred.passed, bool) else str(pred.passed).lower() == "true"
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {**verdict, "verifier": f"rule-based (dspy failed: {self.last_error[:120]})"}
        verdict["checks"].append({"name": "dspy:goal_satisfied", "passed": ok, "detail": str(pred.reason)[:200]})
        verdict["passed"] = ok
        verdict["score"] = round(sum(c["passed"] for c in verdict["checks"]) / len(verdict["checks"]), 3)
        verdict["reasons"] = [f"{c['name']}: {c['detail']}" for c in verdict["checks"] if not c["passed"]]
        return {**verdict, "verifier": self.name}
