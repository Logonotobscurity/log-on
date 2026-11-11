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
