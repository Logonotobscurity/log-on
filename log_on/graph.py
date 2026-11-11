r"""The Safe Orchestrator as a LangGraph StateGraph.

    intake -> policy --refused--------------------------------------------> audit -> END
                     \-> planning --no plan / denied tool-----------------> audit
                                  \-> permission --needs approval--> approval (interrupt) --denied--> audit
                                                 \---------------------------------\-> action -> validation -> audit
"""
from __future__ import annotations

import functools
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import permissions, policy
from .audit import AuditLog
from .guards import Guard, make_guard, redact_text
from .mcp_client import call_tools
from .planner import Planner, get_planner
from .state import AgentState


class Orchestrator:
    def __init__(self, planner: Planner | None = None, guard: Guard | None = None,
                 audit_path: str | os.PathLike = "runs/audit.jsonl", outbox: str | os.PathLike = "runs/outbox.jsonl",
                 checkpointer=None, tool_caller: Callable[..., dict[str, Any]] = call_tools):
        self.planner = planner or get_planner()
        self.guard = guard or make_guard()
        self.audit = AuditLog(audit_path, redact=redact_text)
        self.outbox = str(Path(outbox).resolve())
        self.tool_caller = tool_caller
        self.checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
        self.graph = self._build()

    # --- audit decorator: one record per node execution (+ interrupts) ------------------------------
    def _audited(self, name: str, fn: Callable[[AgentState], dict]) -> Callable[[AgentState], dict]:
        @functools.wraps(fn)
        def wrapper(state: AgentState) -> dict:
            try:
                delta = fn(state)
            except GraphInterrupt:
                self.audit.record(state.get("run_id", "?"), name, "interrupted",
                                  {"reason": "human approval required", "plan": state.get("plan")})
                raise
            self.audit.record(state.get("run_id", "?"), name, "completed", delta)
            return delta
        return wrapper

    # --- nodes -------------------------------------------------------------------------------------
    def intake(self, state: AgentState) -> dict:
        goal = " ".join((state.get("goal") or "").split())  # normalise whitespace
        return {"goal": goal, "run_id": state.get("run_id") or uuid.uuid4().hex[:12],
                "user": state.get("user", "anonymous"), "status": "running",
                "tool_results": [], "validated_results": [], "violations": []}

    def policy_check(self, state: AgentState) -> dict:
        decision = policy.check_input(state["goal"])
        out: dict[str, Any] = {"policy": decision}
        if not decision["allowed"]:
            out.update(status="refused", final_output="Refused by input policy: " + " ".join(decision["reasons"]))
        return out

    def planning(self, state: AgentState) -> dict:
        steps = self.planner.plan(state["goal"])
        out: dict[str, Any] = {"plan": steps, "planner": getattr(self.planner, "name", "custom")}
        if getattr(self.planner, "last_error", None):
            out["planner"] += f" (fell back to rules: {self.planner.last_error})"
        if not steps:
            out.update(status="no_plan", final_output="I couldn't map that goal to an allowed tool.")
        return out

    def permission(self, state: AgentState) -> dict:
        decision = permissions.evaluate(state["plan"])
        out: dict[str, Any] = {"permission": decision}
        if not decision["allowed"]:
            out.update(status="refused", final_output=f"Tools not permitted: {decision['denied_tools']}")
        elif decision["needs_approval"]:
            out["status"] = "awaiting_approval"
        return out

    def approval(self, state: AgentState) -> dict:
        steps = [state["plan"][i] for i in state["permission"]["approval_steps"]]
        # Pauses the graph; state is saved by the checkpointer. Resume with Command(resume={...}).
        decision = interrupt({"question": "Approve these externally visible actions?", "steps": steps,
                              "run_id": state["run_id"]})
        if isinstance(decision, bool):
            decision = {"approved": decision}
        decision = {"approved": bool(decision.get("approved")), "approver": decision.get("approver", "unknown"),
                    "note": decision.get("note", "")}
        out: dict[str, Any] = {"approval": decision}
        if decision["approved"]:
            out["status"] = "running"
        else:
            out.update(status="denied", final_output="Action not executed: approval was denied.")
        return out

    def action(self, state: AgentState) -> dict:
        calls = [(s["tool"], s.get("args", {})) for s in state["plan"]]
        res = self.tool_caller(calls, outbox=self.outbox, egress_filter=redact_text)
        mismatches = permissions.annotation_mismatches(res.get("tools", {}))
        return {"tool_results": res["results"], "permission": {**state["permission"], "annotation_mismatches": mismatches}}

    def validation(self, state: AgentState) -> dict:
        validated, violations, blocked = [], [], False
        for r in state["tool_results"]:
            outcome = self.guard.validate(r["output"], {"tool": r["tool"]})
            for v in outcome.violations:
                violations.append({"tool": r["tool"], **v})
            if outcome.blocked:
                blocked = True
                validated.append({"tool": r["tool"], "output": "[BLOCKED by output guardrail]"})
            else:
                validated.append({"tool": r["tool"], "output": outcome.output or ""})
        status = "blocked" if blocked else "completed"
        final = "\n".join(f"{v['tool']}: {v['output']}" for v in validated)
        if blocked:
            why = "; ".join(f"{v['validator']} ({v['message']})" for v in violations if v["on_fail"] == "exception")
            final = f"Output withheld: a guardrail blocked unsafe content [{why}].\n" + final
        return {"validated_results": validated, "violations": violations, "status": status,
                "final_output": final, "guard_backend": getattr(self.guard, "backend", "native")}

    def finalize(self, state: AgentState) -> dict:
        return {"status": state.get("status", "completed")}

    # --- routing -----------------------------------------------------------------------------------
    @staticmethod
    def _after_policy(state: AgentState) -> str:
        return "audit" if state["status"] == "refused" else "planning"

    @staticmethod
    def _after_planning(state: AgentState) -> str:
        return "audit" if state["status"] == "no_plan" else "permission"

    @staticmethod
    def _after_permission(state: AgentState) -> str:
        if state["status"] == "refused":
            return "audit"
        return "approval" if state["permission"]["needs_approval"] else "action"

    @staticmethod
    def _after_approval(state: AgentState) -> str:
        return "action" if state["approval"]["approved"] else "audit"

    def _build(self):
        g = StateGraph(AgentState)
        for name, fn in [("intake", self.intake), ("policy", self.policy_check), ("planning", self.planning),
                         ("permission", self.permission), ("approval", self.approval), ("action", self.action),
                         ("validation", self.validation), ("audit", self.finalize)]:
            g.add_node(name, self._audited(name, fn))
        g.add_edge(START, "intake")
        g.add_edge("intake", "policy")
        g.add_conditional_edges("policy", self._after_policy, ["planning", "audit"])
        g.add_conditional_edges("planning", self._after_planning, ["permission", "audit"])
        g.add_conditional_edges("permission", self._after_permission, ["approval", "action", "audit"])
        g.add_conditional_edges("approval", self._after_approval, ["action", "audit"])
        g.add_edge("action", "validation")
        g.add_edge("validation", "audit")
        g.add_edge("audit", END)
        return g.compile(checkpointer=self.checkpointer)

    # --- convenience API ---------------------------------------------------------------------------
    def start(self, goal: str, run_id: str | None = None, user: str = "rhonda") -> dict:
        run_id = run_id or uuid.uuid4().hex[:12]
        cfg = {"configurable": {"thread_id": run_id}}
        result = self.graph.invoke({"goal": goal, "run_id": run_id, "user": user}, cfg)
        return self._summarise(run_id, result)

    def resume(self, run_id: str, approved: bool, approver: str = "rhonda", note: str = "") -> dict:
        from langgraph.types import Command
        cfg = {"configurable": {"thread_id": run_id}}
        result = self.graph.invoke(Command(resume={"approved": approved, "approver": approver, "note": note}), cfg)
        return self._summarise(run_id, result)

    def _summarise(self, run_id: str, result: dict) -> dict:
        interrupts = result.get("__interrupt__")
        out = {k: v for k, v in result.items() if k != "__interrupt__"}
        out["run_id"] = run_id
        if interrupts:
            out["status"] = "awaiting_approval"
            out["pending_approval"] = interrupts[0].value
        return out


def pretty(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)
