"""DSPy signatures and modules for LOG_ON's planning and verification steps (DSPy course: signatures, modules).

The *signature* says what goes in and out; DSPy writes the prompt. The optimiser then improves the prompt
(instructions and/or few-shot demos) against a metric, instead of us hand-editing SYSTEM_PROMPT.
Outputs stay plain JSON strings, parsed and allowlist-checked by the same code as the hand-written planner
(`LLMPlanner.parse`), so an optimised program cannot widen what the agent is allowed to do.
"""
from __future__ import annotations

import json

import dspy

TOOLS_DOC = """calculator(expression: str)  - arithmetic only
lookup_customer(customer_id: str)  - CRM record, ids look like C-1001
get_report(name: str)  - 'q3 revenue' or 'weekly ops'
send_report(recipient: str, subject: str, body: str)  - email a report; body is usually "{{steps[0].output}}" after get_report
graph_query(question: str, entity_ids: list[str])  - business knowledge graph (suppliers, contracts, customers), read-only
find_path(source: str, target: str, max_hops: int)  - how two graph entities (by id) are connected
entity_lookup(name: str)  - profile of one customer / supplier / contract / person"""


class PlanGoal(dspy.Signature):
    """You are the planning module of a safe business agent. Turn the user's goal into the shortest list of
    tool calls that achieves it, using ONLY the listed tools. Use entity ids from the context for graph tools.
    Return [] if no listed tool fits. Never invent personal data (emails, phone numbers, ID numbers)."""

    goal: str = dspy.InputField()
    context: str = dspy.InputField(desc="JSON: linked knowledge-graph entities (id, name, label) and remembered facts")
    tools: str = dspy.InputField(desc="the only tools you may call")
    steps_json: str = dspy.OutputField(desc='JSON list, e.g. [{"tool": "calculator", "args": {"expression": "2+2"}}]')


class VerifyOutcome(dspy.Signature):
    """You check an agent's work. Decide whether the tool results actually satisfy the user's goal.
    Fail results that are errors, off-topic, incomplete or that ignore part of the goal. Redacted values such as
    <EMAIL> or <NIN> are expected and are not a failure."""

    goal: str = dspy.InputField()
    plan_json: str = dspy.InputField()
    results_json: str = dspy.InputField()
    passed: bool = dspy.OutputField()
    reason: str = dspy.OutputField(desc="one short sentence")


class PlannerProgram(dspy.Module):
    def __init__(self):
        super().__init__()
        self.plan = dspy.Predict(PlanGoal)

    def forward(self, goal: str, context: str = "{}", tools: str = TOOLS_DOC):
        return self.plan(goal=goal, context=context, tools=tools)


class VerifierProgram(dspy.Module):
    def __init__(self):
        super().__init__()
        self.verify = dspy.Predict(VerifyOutcome)

    def forward(self, goal: str, plan_json: str, results_json: str):
        return self.verify(goal=goal, plan_json=plan_json, results_json=results_json)


def context_str(context: dict | None) -> str:
    """Compact, deterministic context for the planner prompt (same fields the LLMPlanner sees)."""
    c = context or {}
    ents = [{k: e.get(k) for k in ("id", "name", "label", "from") if e.get(k)} for e in c.get("entities", [])]
    out = {"entities": ents}
    if c.get("memories"):
        out["memories"] = c["memories"][:3]
    return json.dumps(out, separators=(",", ":"))


def to_dspy_lm(provider, **kw) -> "dspy.LM":
    """A dspy.LM pointed at the same OpenAI-compatible endpoint as `log_on.serving.Provider`.

    model_type="chat": DSPy 3.5 defaults to the OpenAI *Responses* API for openai/* models, which llama.cpp and
    vLLM only partly implement (few-shot demos fail with HTTP 400); /v1/chat/completions works everywhere."""
    c = provider.config
    return dspy.LM(f"openai/{c.model}", model_type="chat", api_base=c.base_url, api_key=c.api_key or "local", temperature=0,
                   max_tokens=kw.pop("max_tokens", 256), cache=kw.pop("cache", False), num_retries=1, **kw)
