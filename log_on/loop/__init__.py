"""Phase 3 autonomous loop: verify -> revise -> retry (bounded) -> escalate to a human."""
from dataclasses import dataclass, field

from ..telemetry.cost import Budget
from .reviser import PlannerReviser, RuleReviser, get_reviser, plan_key  # noqa: F401
from .verifier import LLMVerifier, RuleVerifier, get_verifier  # noqa: F401


@dataclass
class LoopConfig:
    max_attempts: int = 3                       # attempts before escalating to a human
    budget: Budget = field(default_factory=Budget.from_env)
    verifier: object | None = None              # default: get_verifier() (LLM if a key is set, else rules)
    reviser: object | None = None               # default: get_reviser(planner)

    @classmethod
    def from_env(cls, **kw) -> "LoopConfig":
        import os
        kw.setdefault("max_attempts", int(os.getenv("LOG_ON_MAX_ATTEMPTS", "3")))
        return cls(**kw)
