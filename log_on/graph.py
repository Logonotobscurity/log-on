r"""The Safe Orchestrator as a LangGraph StateGraph.

Phase 1 (always):
    intake -> policy --refused--------------------------------------------> audit -> END
                     \-> planning --no plan / denied tool-----------------> audit
                                  \-> permission --needs approval--> approval (interrupt) --denied--> audit
                                                 \---------------------------------\-> action -> validation -> audit

Phase 2 (when `memory=` and/or `graph_path=`/`graph_backend=` are given), grounding nodes run *after* the
input policy (refused goals never touch memory or the graph) and *before* planning; memory writes happen
after output validation, so only guard-checked content is ever stored:
    policy -> memory_retrieval -> knowledge_retrieval -> planning -> ... -> validation -> memory_update -> audit
              planning --memory intent (remember / from now on / recall)--> validation -> memory_update
              approval --denied--> memory_update (procedural lesson) -> audit
"""
from __future__ import annotations

import functools
import inspect
import json
import os
import re
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
from .planner import SELF_REF_RE, Planner, get_planner
from .state import AgentState

STOPWORDS = {"which", "what", "that", "with", "from", "their", "this", "about", "does", "have", "whose", "they",
             "them", "there", "where", "when", "will", "would", "should", "could", "please", "tell", "show"}


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in STOPWORDS}


def _accepts_context(planner) -> bool:
    try:
        return len(inspect.signature(planner.plan).parameters) >= 2
    except (TypeError, ValueError):
        return False


class Orchestrator:
    def __init__(self, planner: Planner | None = None, guard: Guard | None = None,
                 audit_path: str | os.PathLike = "runs/audit.jsonl", outbox: str | os.PathLike = "runs/outbox.jsonl",
                 checkpointer=None, tool_caller: Callable[..., dict[str, Any]] = call_tools,
                 memory=None, graph_path: str | os.PathLike | None = None, graph_backend: str | None = None):
        self.memory = memory  # MemoryManager | None (Phase 2 long-term memory)
        self.graph_backend = graph_backend
        self.graph_path = str(Path(graph_path).resolve()) if graph_path else None
        self.knowledge_enabled = bool(graph_path or graph_backend)
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

    # --- Phase 2 grounding nodes ---------------------------------------------------------------------
    def memory_retrieval(self, state: AgentState) -> dict:
        from .memory import parse_memory_intent
        user, goal = state.get("user", "anonymous"), state["goal"]
        intent = parse_memory_intent(goal)
        keep = ("key", "text", "score")
        sem = [{k: m.get(k) for k in keep} for m in self.memory.search(user, goal, "semantic", limit=3)]
        epi = [{k: m.get(k) for k in keep} for m in self.memory.search(user, goal, "episodic", limit=2)]
        proc = {k: v.get("value") for k, v in self.memory.instructions(user).items()}
        return {"memory": {"intent": intent, "semantic": sem, "episodic": epi, "procedural": proc,
                           "embedder": self.memory.embedder_name}}

    def _open_graph(self):
        from .kg import make_graph_store
        return make_graph_store(self.graph_backend, self.graph_path, read_only=True)

    def _graph_env(self) -> dict[str, str]:
        env = {}
        if self.graph_backend:
            env["LOG_ON_GRAPH_BACKEND"] = self.graph_backend
        if self.graph_path:
            env["LOG_ON_GRAPH_PATH"] = self.graph_path
        return env

    def knowledge_retrieval(self, state: AgentState) -> dict:
        from .kg.qa import link_entities
        goal = state["goal"]
        try:
            with self._open_graph() as g:
                ents = [{**e, "from": "goal"} for e in link_entities(g, goal)]
                # "my focus customer" -> resolve through semantic memory, but only for self-references and
                # only from memories that share content words with the goal (avoids unrelated recall hijacking).
                if SELF_REF_RE.search(goal):
                    words = _content_words(goal)
                    for m in (state.get("memory") or {}).get("semantic", []):
                        if words & _content_words(m["text"]):
                            for e in link_entities(g, m["text"]):
                                if all(e["id"] != x["id"] for x in ents):
                                    ents.append({**e, "from": "memory", "memory_key": m["key"]})
                            break
                backend = g.backend
            return {"knowledge": {"available": True, "backend": backend, "entities": ents}}
        except Exception as exc:  # noqa: BLE001 - a missing graph must not break the run
            return {"knowledge": {"available": False, "error": f"{type(exc).__name__}: {exc}", "entities": []}}

    def _memory_op(self, state: AgentState, intent: dict) -> dict:
        from .memory import MemoryWriteRejected
        user = state.get("user", "anonymous")
        if intent["kind"] in ("fact", "instruction"):
            try:
                clean, tags = self.memory.guard(intent["text"])
                msg = ("Noted. I'll remember: " if intent["kind"] == "fact" else "Understood. From now on: ") + clean
                if tags:
                    msg += f" (personal data removed before storing: {', '.join(tags)})"
            except MemoryWriteRejected as exc:
                msg = f"I won't store that: {exc}."
        else:
            facts = self.memory.all_facts(user)
            eps = self.memory.recent_episodes(user, 5)
            proc = self.memory.instructions(user)
            lines = ["Facts I remember:"] + ([f"  - {f['text']}" for f in facts] or ["  (none)"])
            lines += ["Recent requests:"] + ([f"  - {e['goal']} -> {e['status']}" for e in eps] or ["  (none)"])
            lines += ["Standing instructions:"] + [f"  - {v['text']}" for k, v in proc.items()
                                                   if not k.startswith("denied-recipient-")]
            msg = "\n".join(lines)
        return {"plan": [], "planner": "memory", "status": "running",
                "tool_results": [{"tool": "memory", "args": {"intent": intent["kind"]}, "ok": True, "output": msg}]}

    def planning(self, state: AgentState) -> dict:
        intent = (state.get("memory") or {}).get("intent")
        if self.memory is not None and intent:
            return self._memory_op(state, intent)
        if (self.memory is not None or self.knowledge_enabled) and _accepts_context(self.planner):
            kn = state.get("knowledge") or {}
            mem = state.get("memory") or {}
            context = {"entities": kn.get("entities", []), "graph_available": kn.get("available", False),
                       "memories": [m["text"] for m in mem.get("semantic", [])],
                       "instructions": mem.get("procedural", {})}
            steps = self.planner.plan(state["goal"], context)
        else:
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
        payload = {"question": "Approve these externally visible actions?", "steps": steps, "run_id": state["run_id"]}
        if self.memory is not None:  # procedural memory: surface earlier denials for the same recipient
            warns = []
            for st in steps:
                rcpt = (st.get("args") or {}).get("recipient")
                d = self.memory.denial_for(state.get("user", "anonymous"), rcpt) if rcpt else None
                if d:
                    warns.append(d["text"])
            if warns:
                payload["memory_warnings"] = warns
        decision = interrupt(payload)
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
        kwargs = {"extra_env": self._graph_env()} if self.knowledge_enabled else {}
        res = self.tool_caller(calls, outbox=self.outbox, egress_filter=redact_text, **kwargs)
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
        style = ((state.get("memory") or {}).get("procedural") or {}).get("answer_style")
        if style == "brief":  # procedural memory changes how answers are rendered
            for v in validated:
                if v["tool"] == "memory":
                    continue
                lines = v["output"].splitlines()
                head = lines[0].rstrip(":") + ("." if lines and lines[0].endswith(":") else "") if lines else ""
                keep = [head] + [l for l in lines[1:] if l.startswith("Sources:")]
                v["output"] = "\n".join(keep)
        final = "\n".join(f"{v['tool']}: {v['output']}" for v in validated)
        if blocked:
            why = "; ".join(f"{v['validator']} ({v['message']})" for v in violations if v["on_fail"] == "exception")
            final = f"Output withheld: a guardrail blocked unsafe content [{why}].\n" + final
        return {"validated_results": validated, "violations": violations, "status": status,
                "final_output": final, "guard_backend": getattr(self.guard, "backend", "native")}

    def memory_update(self, state: AgentState) -> dict:
        from .memory import MemoryWriteRejected
        user, run_id = state.get("user", "anonymous"), state["run_id"]
        intent = (state.get("memory") or {}).get("intent") or {}
        writes: list[dict] = []
        status = state.get("status", "")
        try:
            if intent.get("kind") == "fact" and status == "completed":
                w = self.memory.remember_fact(user, intent["text"], run_id)
                writes.append({"namespace": "semantic", "key": w["key"], "text": w["text"]})
            elif intent.get("kind") == "instruction" and status == "completed":
                w = self.memory.set_instruction(user, intent["text"], run_id)
                writes.append({"namespace": "procedural", "key": w["key"], "text": w["text"]})
        except MemoryWriteRejected as exc:
            writes.append({"namespace": "semantic", "rejected": str(exc)})
        if status == "denied":
            for st in state.get("plan", []):
                rcpt = (st.get("args") or {}).get("recipient")
                if rcpt:
                    w = self.memory.record_denial(user, rcpt, st["tool"], (state.get("approval") or {}).get("note", ""),
                                                  run_id)
                    writes.append({"namespace": "procedural", "key": w["key"], "text": w["text"]})
        tools = [s["tool"] for s in state.get("plan", [])] or [r["tool"] for r in state.get("tool_results", [])]
        ep = self.memory.add_episode(user, run_id, state["goal"], status, tools,
                                     (state.get("final_output") or "")[:300], intent.get("kind"))
        writes.append({"namespace": "episodic", "key": ep["key"], "text": ep["text"]})
        return {"memory_writes": writes}

    def finalize(self, state: AgentState) -> dict:
        return {"status": state.get("status", "completed")}

    # --- routing -----------------------------------------------------------------------------------
    def _after_policy(self, state: AgentState) -> str:
        if state["status"] == "refused":
            return "audit"
        if self.memory is not None:
            return "memory_retrieval"
        return "knowledge_retrieval" if self.knowledge_enabled else "planning"

    def _after_planning(self, state: AgentState) -> str:
        if state["status"] == "no_plan":
            return "memory_update" if self.memory is not None else "audit"
        if state.get("planner") == "memory":
            return "validation"
        return "permission"

    @staticmethod
    def _after_permission(state: AgentState) -> str:
        if state["status"] == "refused":
            return "audit"
        return "approval" if state["permission"]["needs_approval"] else "action"

    def _after_approval(self, state: AgentState) -> str:
        if state["approval"]["approved"]:
            return "action"
        return "memory_update" if self.memory is not None else "audit"

    def _build(self):
        g = StateGraph(AgentState)
        nodes = [("intake", self.intake), ("policy", self.policy_check), ("planning", self.planning),
                 ("permission", self.permission), ("approval", self.approval), ("action", self.action),
                 ("validation", self.validation), ("audit", self.finalize)]
        mem, kg = self.memory is not None, self.knowledge_enabled
        if mem:
            nodes += [("memory_retrieval", self.memory_retrieval), ("memory_update", self.memory_update)]
        if kg:
            nodes += [("knowledge_retrieval", self.knowledge_retrieval)]
        for name, fn in nodes:
            g.add_node(name, self._audited(name, fn))
        g.add_edge(START, "intake")
        g.add_edge("intake", "policy")
        grounding = (["memory_retrieval"] if mem else []) + (["knowledge_retrieval"] if kg else [])
        g.add_conditional_edges("policy", self._after_policy, [grounding[0] if grounding else "planning", "audit"])
        for a, b in zip(grounding, grounding[1:] + ["planning"]):
            g.add_edge(a, b)
        after_plan = ["permission", "audit"] + (["memory_update", "validation"] if mem else [])
        g.add_conditional_edges("planning", self._after_planning, after_plan)
        g.add_conditional_edges("permission", self._after_permission, ["approval", "action", "audit"])
        g.add_conditional_edges("approval", self._after_approval, ["action", "memory_update" if mem else "audit"])
        g.add_edge("action", "validation")
        g.add_edge("validation", "memory_update" if mem else "audit")
        if mem:
            g.add_edge("memory_update", "audit")
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
