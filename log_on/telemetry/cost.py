"""Token and cost estimates, plus per-run budgets (roadmap ticket O-1).

Token counts: when an OpenAI-compatible endpoint returns `usage`, those numbers are used. Otherwise
tokens are *estimated* as ceil(chars / 4), the usual rule of thumb for English text. Tool outputs are
counted as "context tokens", since an LLM planner/verifier would have to read them on the next turn.

Prices are USD per 1M tokens (input, output). They are estimates for budgeting, not billing; edit the
table or set LOG_ON_PRICE_IN / LOG_ON_PRICE_OUT to override for your model.
"""
from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass
from typing import Any

PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "rule-based": (0.0, 0.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "grok-3-mini": (0.30, 0.50),
    "default": (1.00, 3.00),  # unknown model: deliberately pessimistic
}


def est_tokens(text: Any) -> int:
    s = text if isinstance(text, str) else str(text or "")
    return math.ceil(len(s) / 4) if s else 0


SELF_HOSTED = {"vllm", "llamacpp", "ollama", "fake"}  # Phase 4: no per-token bill (hardware cost is fixed)


def price_for(model: str | None, provider: str | None = None) -> tuple[float, float]:
    if provider in SELF_HOSTED and not os.getenv("LOG_ON_PRICE_IN"):
        return 0.0, 0.0
    if os.getenv("LOG_ON_PRICE_IN") or os.getenv("LOG_ON_PRICE_OUT"):
        return float(os.getenv("LOG_ON_PRICE_IN", "0")), float(os.getenv("LOG_ON_PRICE_OUT", "0"))
    if not model:
        return PRICES_PER_MTOK["rule-based"]
    return PRICES_PER_MTOK.get(model, PRICES_PER_MTOK["default"])


def llm_cost(model: str | None, prompt_tokens: int, completion_tokens: int, provider: str | None = None) -> float:
    pin, pout = price_for(model, provider)
    return (prompt_tokens * pin + completion_tokens * pout) / 1_000_000


def empty_usage() -> dict[str, Any]:
    return {"steps": 0, "llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "context_tokens": 0,
            "total_tokens": 0, "tool_calls": 0, "est_cost_usd": 0.0}


def add_usage(base: dict | None, *, steps: int = 0, llm: dict | None = None, tool_calls: int = 0,
              context_tokens: int = 0) -> dict[str, Any]:
    u = {**empty_usage(), **(base or {})}
    u["steps"] += steps
    u["tool_calls"] += tool_calls
    u["context_tokens"] += context_tokens
    if llm:
        u["llm_calls"] += llm.get("calls", 1)
        u["prompt_tokens"] += llm.get("prompt_tokens", 0)
        u["completion_tokens"] += llm.get("completion_tokens", 0)
        u["est_cost_usd"] = round(u["est_cost_usd"] + llm_cost(llm.get("model"), llm.get("prompt_tokens", 0),
                                                               llm.get("completion_tokens", 0),
                                                               llm.get("provider")), 6)
    u["total_tokens"] = u["prompt_tokens"] + u["completion_tokens"] + u["context_tokens"]
    return u


@dataclass
class Budget:
    """Per-run limits, enforced by the graph's routers before every transition (see graph.py)."""
    max_steps: int = 40          # node executions (all attempts together)
    max_tool_calls: int = 10     # MCP tool calls, counting the ones a plan is *about* to make
    max_tokens: int = 20_000     # prompt + completion + context tokens
    max_cost_usd: float = 0.05   # estimated LLM spend per run

    @classmethod
    def from_env(cls, **overrides) -> "Budget":
        b = cls(max_steps=int(os.getenv("LOG_ON_MAX_STEPS", cls.max_steps)),
                max_tool_calls=int(os.getenv("LOG_ON_MAX_TOOL_CALLS", cls.max_tool_calls)),
                max_tokens=int(os.getenv("LOG_ON_MAX_TOKENS", cls.max_tokens)),
                max_cost_usd=float(os.getenv("LOG_ON_MAX_COST_USD", cls.max_cost_usd)))
        for k, v in overrides.items():
            setattr(b, k, v)
        return b

    def exceeded(self, usage: dict | None, pending_tool_calls: int = 0) -> list[str]:
        u = {**empty_usage(), **(usage or {})}
        out = []
        if u["steps"] >= self.max_steps:
            out.append(f"steps {u['steps']} >= {self.max_steps}")
        if u["tool_calls"] + pending_tool_calls > self.max_tool_calls:
            out.append(f"tool calls {u['tool_calls']}+{pending_tool_calls} > {self.max_tool_calls}")
        if u["total_tokens"] > self.max_tokens:
            out.append(f"tokens {u['total_tokens']} > {self.max_tokens}")
        if u["est_cost_usd"] > self.max_cost_usd:
            out.append(f"cost ${u['est_cost_usd']:.4f} > ${self.max_cost_usd}")
        return out

    def as_dict(self) -> dict:
        return asdict(self)
