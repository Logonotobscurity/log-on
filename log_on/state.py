"""Shared graph state (LangGraph `StateGraph` schema)."""
from __future__ import annotations

from typing import Any, TypedDict


class PlanStep(TypedDict, total=False):
    tool: str
    args: dict[str, Any]
    reason: str


class AgentState(TypedDict, total=False):
    run_id: str
    user: str
    goal: str
    status: str  # running | refused | no_plan | awaiting_approval | denied | completed | blocked | error
    #             Phase 3: unverified (loop deciding) | escalated (human aborted) | budget_exceeded
    policy: dict[str, Any]
    planner: str
    plan: list[PlanStep]
    permission: dict[str, Any]
    approval: dict[str, Any]
    tool_results: list[dict[str, Any]]
    validated_results: list[dict[str, Any]]
    violations: list[dict[str, Any]]
    guard_backend: str
    final_output: str
    # Phase 2
    memory: dict[str, Any]          # retrieved memories + detected memory intent
    knowledge: dict[str, Any]       # linked graph entities (grounding for the planner)
    memory_writes: list[dict[str, Any]]
    # Phase 3
    usage: dict[str, Any]           # token / cost / step ledger (telemetry.cost.add_usage)
    attempt: int                    # 1-based attempt number of the current plan
    attempt_limit: int              # escalate to a human when a failed attempt reaches this number
    attempts: list[dict[str, Any]]  # every attempt: plan, redacted results, verifier verdict
    tried_plans: list[str]          # canonical keys of executed plans (the reviser never repeats one)
    verification: dict[str, Any]    # latest verifier verdict
    revision: dict[str, Any]        # latest reviser notes
    escalation: dict[str, Any]      # the human's decision after escalation
    budget: dict[str, Any]          # limits and, if stopped, which were exceeded
