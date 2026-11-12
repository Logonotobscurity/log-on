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

Phase 3 (when `loop=` is given): a self-checking loop after output validation, run budgets on every
costly transition, and OpenTelemetry tracing of every node (tracing is on in all phases):
    validation -> verification --passed / guardrail-blocked--> (memory_update) -> audit
                              \--failed, attempt < limit--> replan --new plan--> permission -> [approval] -> action ...
                              \--failed, limit reached---> escalation (interrupt) --accept/abort--> (memory_update) -> audit
                                                         replan --no safe repair--/            \--retry + revised goal--> policy
    any router about to enter planning / replan / approval / action / verification --over budget--> budget_stop -> audit
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
from .telemetry.cost import add_usage, est_tokens
from .telemetry.tracing import RunTracer

# Phase 3: transitions into these nodes spend tokens, tool calls or a human's attention -> budget-checked.
COSTLY = {"planning", "replan", "approval", "action", "verification"}
IN_SCOPE_RE = re.compile(r"\b(calculat\w*|compute|sum|total|revenue|reports?|customers?|suppliers?|contracts?|"
                         r"exposure|subsidiar\w*|entit\w*|C-\d{3,})\b|\d\s*[-+*/^%]\s*\d", re.I)


PII_ASK_RE = re.compile(r"\b(?:NIN|BVN|card numbers?|phone numbers?|emails?|passwords?|passports?|"
                        r"date of birth|addresses|personal data|records?)\b", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
CODE_RE = re.compile(r"__\w+__|\bimport\b|\b(?:os|sys|subprocess)\.|\b(?:eval|exec|system|open)\s*\(|[`;|&$]", re.I)


def in_scope(goal: str) -> bool:
    """Is the goal a benign request about what this agent's tools cover (maths, reports, customers, the business
    graph)? Out-of-scope goals ("write me a poem") and code smuggled into a request ("Calculate __import__('os')...")
    still end in no_plan; benign in-scope goals a planner cannot map go to a human."""
    from .permissions import is_trusted_address
    from .policy import normalize
    g = goal or ""
    n = normalize(g)
    bare = EMAIL_RE.sub("", g)
    obfuscated = re.sub(r"\s+", " ", bare).strip() != normalize(bare)  # leetspeak, homoglyphs, zero-width ...
    if (CODE_RE.search(g) or obfuscated or PII_ASK_RE.search(n)
            or any(not is_trusted_address(a) for a in EMAIL_RE.findall(g + " " + n))):
        return False  # code, disguise, personal-data asks or outside addresses are not "benign": nobody is paged
    return bool(IN_SCOPE_RE.search(g) or IN_SCOPE_RE.search(n))


INTERRUPT_REASON = {"approval": "human approval required", "escalation": "escalated to a human: result not verified"}

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
                 memory=None, graph_path: str | os.PathLike | None = None, graph_backend: str | None = None,
                 loop=None, tracer: RunTracer | bool | None = None, trace_path: str | os.PathLike | None = None,
                 quota=None):
        """`loop`: Phase 3 LoopConfig (or True for defaults from env). `tracer`: a RunTracer, False to disable,
        or None for the default (JSONL next to the audit log, plus exporters from LOG_ON_TRACE_EXPORTERS)."""
        from .loop import LoopConfig, get_reviser, get_verifier
        self.memory = memory  # MemoryManager | None (Phase 2 long-term memory)
        self.graph_backend = graph_backend
        self.graph_path = str(Path(graph_path).resolve()) if graph_path else None
        self.knowledge_enabled = bool(graph_path or graph_backend)
        self.planner = planner or get_planner()
        self.guard = guard or make_guard()
        from .provenance import stamp
        self.audit = AuditLog(audit_path, redact=redact_text, provenance=lambda: stamp(self.planner))  # O-3
        self.outbox = str(Path(outbox).resolve())
        self.tool_caller = tool_caller
        self.checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
        # Post-Phase-4 step 3: per-user/day budgets (UserQuota, False to disable, None = from env; off if no limits set)
        if quota is None:
            from .quotas import UserQuota
            quota = UserQuota.from_env(Path(audit_path).parent)
        self.quota = quota or None
        # Phase 3
        self.loop = LoopConfig.from_env() if loop is True else (loop or None)
        if self.loop is not None:
            self.budget = self.loop.budget
            self.verifier = self.loop.verifier or get_verifier()
            self.reviser = self.loop.reviser or get_reviser(self.planner)
        if tracer is False:
            self.tracer = RunTracer(None, exporters=[], redact=redact_text)
        elif isinstance(tracer, RunTracer):
            self.tracer = tracer
        else:
            self.tracer = RunTracer(trace_path or Path(audit_path).parent / "traces.jsonl", redact=redact_text)
        self.graph = self._build()

    # --- audit + trace + cost decorator: one audit record and one span per node execution -------------
    def _audited(self, name: str, fn: Callable[[AgentState], dict]) -> Callable[[AgentState], dict]:
        @functools.wraps(fn)
        def wrapper(state: AgentState) -> dict:
            run_id = state.get("run_id", "?")
            with self.tracer.node_span(name, run_id, attempt=state.get("attempt"), status_in=state.get("status")) as span:
                try:
                    delta = dict(fn(state))
                except GraphInterrupt:
                    self.tracer.set(span, event="interrupted")
                    self.audit.record(run_id, name, "interrupted",
                                      {"reason": INTERRUPT_REASON.get(name, "interrupted"), "plan": state.get("plan"),
                                       "attempt": state.get("attempt")})
                    raise
                delta["usage"] = self._usage(name, state, delta)
                self._trace_node(span, name, delta)
                if self.quota is not None and name != "intake":  # persist this run's usage for the daily budget
                    self.quota.record(state.get("user", "anonymous"), run_id, delta["usage"])
            self.audit.record(run_id, name, "completed", delta)
            return delta
        return wrapper

    def _usage(self, name: str, state: AgentState, delta: dict) -> dict:
        """Phase 3 cost ledger: steps, LLM tokens (reported or estimated), tool calls and context tokens."""
        base = None if name == "intake" else state.get("usage")
        llm, tool_calls, ctx = None, 0, 0
        src = {"planning": self.planner, "replan": getattr(self, "reviser", None),
               "verification": getattr(self, "verifier", None)}.get(name)
        if src is not None and getattr(src, "last_usage", None):
            llm, src.last_usage = src.last_usage, None
        if name == "intake":
            ctx = est_tokens(delta.get("goal", ""))
        if name == "action":
            tool_calls = len(delta.get("tool_results", []))
            ctx = sum(est_tokens(r.get("output")) for r in delta.get("tool_results", []))
        return add_usage(base, steps=1, llm=llm, tool_calls=tool_calls, context_tokens=ctx)

    def _trace_node(self, span, name: str, d: dict) -> None:
        t = self.tracer
        u = d.get("usage") or {}
        t.set(span, status=d.get("status"), **{"usage.total_tokens": u.get("total_tokens"),
                                                "usage.est_cost_usd": u.get("est_cost_usd"), "usage.steps": u.get("steps")})
        if "policy" in d and name == "policy":
            t.set(span, **{"decision.allowed": d["policy"]["allowed"], "decision.rule_ids": d["policy"]["rule_ids"]})
        if name in ("planning", "replan") and d.get("plan") is not None:
            t.set(span, planner=d.get("planner"), **{"plan.tools": [x.get("tool", "") for x in d.get("plan") or []],
                                                     "plan.steps": len(d.get("plan") or [])})
        if name == "permission":
            p = d.get("permission") or {}
            t.set(span, **{"decision.needs_approval": p.get("needs_approval"), "decision.denied_tools": p.get("denied_tools")})
        if name == "approval" and d.get("approval"):
            t.set(span, **{"decision.approved": d["approval"]["approved"], "decision.approver": d["approval"]["approver"]})
        if name == "validation":
            t.set(span, **{"guard.violations": [v["validator"] for v in d.get("violations", [])]})
        if name == "verification" and d.get("verification"):
            v = d["verification"]
            t.set(span, **{"verify.passed": str(v.get("passed")), "verify.score": v.get("score"),
                           "verify.verifier": v.get("verifier"), "verify.reasons": v.get("reasons") or []})
        if name == "escalation" and d.get("escalation"):
            t.set(span, **{"decision.escalation": d["escalation"]["decision"]})
        if name == "budget_stop":
            t.set(span, **{"budget.exceeded": (d.get("budget") or {}).get("exceeded", [])})

    # --- nodes -------------------------------------------------------------------------------------
    def intake(self, state: AgentState) -> dict:
        goal = " ".join((state.get("goal") or "").split())  # normalise whitespace
        out = {"goal": goal, "run_id": state.get("run_id") or uuid.uuid4().hex[:12],
               "user": state.get("user", "anonymous"), "status": "running",
               "tool_results": [], "validated_results": [], "violations": []}
        if self.loop is not None:
            out.update(attempt=1, attempt_limit=self.loop.max_attempts, attempts=[], tried_plans=[],
                       budget=self.budget.as_dict())
        if self.quota is not None:  # step 3: a new run needs runs/tokens/cost left today
            d = self.quota.start_run(out["user"], out["run_id"])
            if not d["allowed"]:
                out.update(status="quota_exceeded", quota=d)
        return out

    def quota_stop(self, state: AgentState) -> dict:
        d = state.get("quota") or self.quota.check(state.get("user", "anonymous"), state.get("run_id", "?"),
                                                  state.get("usage"))
        return {"status": "quota_exceeded", "quota": d, "final_output": self.quota.message(d)}

    def _quota_gate(self, state: AgentState, nxt: str) -> bool:
        """True if the per-user daily budget blocks `nxt` (an LLM-backed step)."""
        if self.quota is None or nxt not in self._llm_steps():
            return False
        return not self.quota.check(state.get("user", "anonymous"), state.get("run_id", "?"),
                                    state.get("usage"))["allowed"]

    def _llm_steps(self) -> set[str]:
        steps = {"planning", "replan"}
        v = getattr(self, "verifier", None)
        if v is not None and (getattr(v, "provider", None) is not None or str(getattr(v, "name", "")).startswith("llm")):
            steps.add("verification")
        return steps

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

    @staticmethod
    def _context(state: AgentState) -> dict:
        kn = state.get("knowledge") or {}
        mem = state.get("memory") or {}
        return {"entities": kn.get("entities", []), "graph_available": kn.get("available", False),
                "memories": [m["text"] for m in mem.get("semantic", [])], "instructions": mem.get("procedural", {})}

    def planning(self, state: AgentState) -> dict:
        intent = (state.get("memory") or {}).get("intent")
        if self.memory is not None and intent:
            return self._memory_op(state, intent)
        if (self.memory is not None or self.knowledge_enabled) and _accepts_context(self.planner):
            steps = self.planner.plan(state["goal"], self._context(state))
        else:
            steps = self.planner.plan(state["goal"])
        out: dict[str, Any] = {"plan": steps, "planner": getattr(self.planner, "name", "custom")}
        if getattr(self.planner, "last_error", None):
            out["planner"] += f" (fell back to rules: {self.planner.last_error})"
        if not steps:
            declined = getattr(self.planner, "last_decline", None)
            if declined:  # the model refused on safety grounds: that is a refusal, not "no plan"
                out.update(status="refused", final_output=f"Refused by the planner: {redact_text(declined)}")
            elif self.loop is not None and in_scope(state["goal"]):
                # a benign, in-scope goal the planner could not map (e.g. "Calculate 10 / 0"): hand it to a human
                # instead of silently giving up
                out.update(status="unplannable", final_output="")
            else:
                out.update(status="no_plan", final_output="I couldn't map that goal to an allowed tool.")
        return out

    def permission(self, state: AgentState) -> dict:
        decision = permissions.evaluate(state["plan"], goal=state.get("goal", ""))
        out: dict[str, Any] = {"permission": decision}
        if decision["denied_tools"]:
            out.update(status="refused", final_output=f"Tools not permitted: {decision['denied_tools']}")
        elif decision.get("invalid_refs"):
            v = decision["invalid_refs"][0]
            out.update(status="refused", final_output=(
                f"Refused: step {v['step']} ({v['tool']}) uses the output of step(s) {v['refs']}, which have not run "
                "yet. The plan is malformed, so nothing was run and no approval was requested."))
        elif decision["provenance_violations"]:
            where = ", ".join(redact_text(v["destination"]) for v in decision["provenance_violations"])
            out.update(status="refused", final_output=(
                f"Refused: the plan would send data to {where}, which you did not give in this request. "
                "I only send to addresses you type yourself."))
        elif decision.get("untrusted_destinations"):
            doms = sorted({v["domain"] for v in decision["untrusted_destinations"]})
            out.update(status="refused", final_output=(
                f"Refused: the plan would send to {', '.join(doms)}, which is outside the trusted domains "
                "(LOG_ON_TRUSTED_DOMAINS). Sending outside the organisation is not allowed, so nothing was run and "
                "no approval was requested."))
        elif decision["dataflow_violations"]:
            v = decision["dataflow_violations"][0]
            out.update(status="refused", final_output=(
                f"Refused: the plan would email personal data from {v['sources']} to an outside domain "
                f"({v['domain']}). Customer records may only be sent to trusted domains (LOG_ON_TRUSTED_DOMAINS)."))
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
        for r in res["results"]:
            out = str(r.get("output") or "")
            self.tracer.tool_span(r.get("tool", "?"), state.get("run_id", "?"), r.pop("started_ns", None),
                                  r.pop("ended_ns", None), ok=bool(r.get("ok", True)),
                                  args=json.dumps(r.get("args", {}), default=str), output_chars=len(out),
                                  output_tokens_est=est_tokens(out), output_preview=out[:200])
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

    # --- Phase 3: self-checking loop -----------------------------------------------------------------
    def verification(self, state: AgentState) -> dict:
        from .loop import plan_key
        attempt = state.get("attempt", 1)
        if state.get("status") == "blocked":
            verdict = {"verifier": "skipped", "passed": None, "score": None, "checks": [],
                       "reasons": ["guardrail block is final and is never retried"]}
        elif state.get("planner") == "memory":
            verdict = {"verifier": "skipped", "passed": True, "score": 1.0, "checks": [], "reasons": []}
        else:
            verdict = self.verifier.verify(state["goal"], state.get("plan", []), state.get("tool_results", []),
                                           state.get("validated_results", []))
        record = {"attempt": attempt, "planner": state.get("planner"),
                  "plan": [{"tool": x.get("tool"), "args": x.get("args", {})} for x in state.get("plan", [])],
                  "results": [{"tool": v["tool"], "output": redact_text(v["output"])[:160]}
                              for v in state.get("validated_results", [])],
                  "verdict": {k: verdict.get(k) for k in ("verifier", "passed", "score", "reasons")}}
        out: dict[str, Any] = {"verification": {**verdict, "attempt": attempt},
                               "attempts": [*state.get("attempts", []), record],
                               "tried_plans": [*state.get("tried_plans", []), plan_key(state.get("plan", []))]}
        if verdict["passed"] is False:
            out["status"] = "unverified"
        return out

    def replan(self, state: AgentState) -> dict:
        verdict = state.get("verification") or {}
        new, notes = self.reviser.revise(state["goal"], state.get("plan", []), verdict, state.get("tool_results", []),
                                         self._context(state), state.get("tried_plans", []))
        out: dict[str, Any] = {"revision": {"reviser": getattr(self.reviser, "name", "custom"), "notes": notes,
                                            "proposed": bool(new)}}
        if new:
            out.update(plan=new, planner=f"{getattr(self.reviser, 'name', 'custom')} reviser", status="running",
                       attempt=state.get("attempt", 1) + 1, tool_results=[], validated_results=[], violations=[])
        return out

    def escalation(self, state: AgentState) -> dict:
        attempt = state.get("attempt", 1)
        unplannable = state.get("status") == "unplannable"
        reasons = (["the planner could not map this goal to an allowed tool"] if unplannable
                   else (state.get("verification") or {}).get("reasons", []))
        question = ("I could not plan this request with the allowed tools. Retry with a revised goal, or abort?"
                    if unplannable else f"I could not verify the result after {attempt} attempt(s). "
                                        "Accept it, retry with a revised goal, or abort?")
        payload = {"question": question,
                   "run_id": state["run_id"], "goal": state["goal"], "reasons": reasons,
                   "options": ["retry", "abort"] if unplannable else ["accept", "retry", "abort"],
                   "attempts": [{"attempt": a["attempt"], "tools": [p["tool"] for p in a["plan"]],
                                 "passed": a["verdict"]["passed"], "reasons": a["verdict"]["reasons"]}
                                for a in state.get("attempts", [])],
                   "last_output": state.get("final_output", "")}
        if (state.get("revision") or {}).get("notes"):
            payload["reviser_notes"] = state["revision"]["notes"]
        raw = interrupt(payload)
        if isinstance(raw, bool):
            raw = {"decision": "accept" if raw else "abort"}
        elif isinstance(raw, str):
            raw = {"decision": raw}
        choice = raw.get("decision") if raw.get("decision") in payload["options"] else "abort"
        dec = {"decision": choice, "by": raw.get("by", raw.get("approver", "unknown")), "note": raw.get("note", "")}
        out: dict[str, Any] = {"escalation": dec}
        if choice == "accept":
            out.update(status="completed",
                       final_output="[accepted by a human without passing verification]\n" + state.get("final_output", ""))
        elif choice == "abort" and unplannable:
            out.update(status="escalated", final_output=(
                "Escalated to a human: I could not plan this request with the allowed tools, and the human aborted."))
        elif choice == "abort":
            last = state.get("final_output", "")
            out.update(status="escalated", final_output=(
                f"Stopped after {attempt} attempt(s): the result could not be verified "
                f"({'; '.join(reasons[:3]) or 'no safe revision'}). Escalated to a human, who aborted."
                + (f"\nLast unverified result:\n{last}" if last else "")))
        else:
            goal = " ".join((raw.get("goal") or state["goal"]).split())
            dec["goal"] = goal
            out.update(goal=goal, status="running", attempt=attempt + 1, attempt_limit=attempt + 1, plan=[],
                       tool_results=[], validated_results=[], violations=[])
        return out

    def budget_stop(self, state: AgentState) -> dict:
        usage = state.get("usage")
        why = self.budget.exceeded(usage) or self.budget.exceeded(usage, len(state.get("plan") or []))
        return {"status": "budget_exceeded", "budget": {**self.budget.as_dict(), "exceeded": why},
                "final_output": f"Stopped: run budget exceeded ({'; '.join(why)}). Nothing further was executed."}

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
        if state["status"] == "unplannable":
            return "escalation"
        if state["status"] in ("no_plan", "refused"):
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

    def _after_verification(self, state: AgentState) -> str:
        done = "memory_update" if self.memory is not None else "audit"
        if (state.get("verification") or {}).get("passed") is not False:
            return done
        return "replan" if state.get("attempt", 1) < state.get("attempt_limit", self.loop.max_attempts) else "escalation"

    @staticmethod
    def _after_replan(state: AgentState) -> str:
        return "permission" if state.get("status") == "running" else "escalation"

    def _after_escalation(self, state: AgentState) -> str:
        if state["escalation"]["decision"] == "retry":
            return "policy"
        return "memory_update" if self.memory is not None else "audit"

    def _link(self, g: StateGraph, src: str, router: Callable | str, dests: list[str]) -> None:
        """Every edge is a traced decision; with the loop on, edges into costly nodes are budget-checked."""
        fixed = router if isinstance(router, str) else None
        guarded = self.loop is not None and any(d in COSTLY for d in dests)
        quota_gated = self.quota is not None and any(d in self._llm_steps() for d in dests)

        def route(state: AgentState) -> str:
            nxt = fixed or router(state)
            if guarded and nxt in COSTLY:
                pending = len(state.get("plan") or []) if nxt in ("approval", "action") else 0
                if self.budget.exceeded(state.get("usage"), pending):
                    nxt = "budget_stop"
            if quota_gated and self._quota_gate(state, nxt):
                nxt = "quota_stop"
            self.tracer.decision("route", **{"from": src, "to": nxt, "status": state.get("status")})
            return nxt
        route.__name__ = f"route_{src}"
        g.add_conditional_edges(src, route, list(dict.fromkeys(dests + (["budget_stop"] if guarded else [])
                                                               + (["quota_stop"] if quota_gated else []))))

    def _build(self):
        g = StateGraph(AgentState)
        nodes = [("intake", self.intake), ("policy", self.policy_check), ("planning", self.planning),
                 ("permission", self.permission), ("approval", self.approval), ("action", self.action),
                 ("validation", self.validation), ("audit", self.finalize)]
        mem, kg, loop = self.memory is not None, self.knowledge_enabled, self.loop is not None
        if mem:
            nodes += [("memory_retrieval", self.memory_retrieval), ("memory_update", self.memory_update)]
        if kg:
            nodes += [("knowledge_retrieval", self.knowledge_retrieval)]
        if loop:
            nodes += [("verification", self.verification), ("replan", self.replan), ("escalation", self.escalation),
                      ("budget_stop", self.budget_stop)]
        if self.quota is not None:
            nodes += [("quota_stop", self.quota_stop)]
        for name, fn in nodes:
            g.add_node(name, self._audited(name, fn))
        done = "memory_update" if mem else "audit"
        g.add_edge(START, "intake")
        if self.quota is not None:
            self._link(g, "intake", lambda st: "quota_stop" if st.get("status") == "quota_exceeded" else "policy",
                       ["policy", "quota_stop"])
            g.add_edge("quota_stop", "audit")
        else:
            self._link(g, "intake", "policy", ["policy"])
        grounding = (["memory_retrieval"] if mem else []) + (["knowledge_retrieval"] if kg else [])
        self._link(g, "policy", self._after_policy, [grounding[0] if grounding else "planning", "audit"])
        for a, b in zip(grounding, grounding[1:] + ["planning"]):
            self._link(g, a, b, [b])
        after_plan = ["permission", "audit"] + (["memory_update", "validation"] if mem else []) + \
            (["escalation"] if loop else [])
        self._link(g, "planning", self._after_planning, after_plan)
        self._link(g, "permission", self._after_permission, ["approval", "action", "audit"])
        self._link(g, "approval", self._after_approval, ["action", done])
        self._link(g, "action", "validation", ["validation"])
        if loop:
            self._link(g, "validation", "verification", ["verification"])
            self._link(g, "verification", self._after_verification, [done, "replan", "escalation"])
            self._link(g, "replan", self._after_replan, ["permission", "escalation"])
            self._link(g, "escalation", self._after_escalation, ["policy", done])
            g.add_edge("budget_stop", "audit")
        else:
            self._link(g, "validation", done, [done])
        if mem:
            g.add_edge("memory_update", "audit")
        g.add_edge("audit", END)
        return g.compile(checkpointer=self.checkpointer)

    # --- convenience API ---------------------------------------------------------------------------
    def _invoke(self, run_id: str, kind: str, payload, **attrs) -> dict:
        cfg = {"configurable": {"thread_id": run_id}}
        with self.tracer.run_span(run_id, kind, loop=self.loop is not None, memory=self.memory is not None,
                                  knowledge=self.knowledge_enabled, **attrs) as span:
            result = self.graph.invoke(payload, cfg)
            out = self._summarise(run_id, result)
            u = out.get("usage") or {}
            self.tracer.set(span, status=out.get("status"), attempt=out.get("attempt"),
                            **{"usage.total_tokens": u.get("total_tokens"), "usage.est_cost_usd": u.get("est_cost_usd"),
                               "usage.steps": u.get("steps"), "usage.tool_calls": u.get("tool_calls")})
        self.tracer.flush()
        return out

    def start(self, goal: str, run_id: str | None = None, user: str = "rhonda") -> dict:
        run_id = run_id or uuid.uuid4().hex[:12]
        return self._invoke(run_id, "start", {"goal": goal, "run_id": run_id, "user": user}, goal=goal, user=user)

    def pending_node(self, run_id: str) -> str | None:
        nxt = self.graph.get_state({"configurable": {"thread_id": run_id}}).next
        return nxt[0] if nxt else None

    def resume(self, run_id: str, approved: bool, approver: str = "rhonda", note: str = "") -> dict:
        """Answer the pending human step. For an escalation, approved=True means accept and False means abort."""
        from langgraph.types import Command
        if self.pending_node(run_id) == "escalation":
            return self.respond(run_id, "accept" if approved else "abort", by=approver, note=note)
        return self._invoke(run_id, "resume", Command(resume={"approved": approved, "approver": approver, "note": note}),
                            decision="approve" if approved else "deny")

    def respond(self, run_id: str, decision: str, goal: str | None = None, by: str = "rhonda", note: str = "") -> dict:
        """Answer an escalation: decision is 'accept', 'retry' (optionally with a revised goal) or 'abort'."""
        from langgraph.types import Command
        payload = {"decision": decision, "by": by, "note": note}
        if goal:
            payload["goal"] = goal
        return self._invoke(run_id, "resume", Command(resume=payload), decision=decision)

    def _summarise(self, run_id: str, result: dict) -> dict:
        interrupts = result.get("__interrupt__")
        out = {k: v for k, v in result.items() if k != "__interrupt__"}
        out["run_id"] = run_id
        if interrupts:
            value = interrupts[0].value
            if isinstance(value, dict) and "options" in value:
                out["status"] = "awaiting_human"
                out["pending_escalation"] = value
            else:
                out["status"] = "awaiting_approval"
                out["pending_approval"] = value
        return out


def pretty(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)
