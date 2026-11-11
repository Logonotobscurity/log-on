"""Pluggable planners.

RuleBasedPlanner (default): deterministic, offline, testable.
LLMPlanner: any OpenAI-compatible /chat/completions endpoint, used only when
LOG_ON_LLM_API_KEY (or OPENAI_API_KEY) is set. Its output is schema-checked
against the tool allowlist and falls back to the rule planner on any error.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Protocol

from .permissions import TOOL_POLICY
from .state import PlanStep


class Planner(Protocol):
    name: str

    def plan(self, goal: str) -> list[PlanStep]: ...


EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
CUST_RE = re.compile(r"\b(C-?\d{4})\b", re.I)
MATH_RE = re.compile(r"[-+*/().%\d\s]*\d[-+*/().%\d\s]*[-+*/%][-+*/().%\d\s]*\d[-+*/().%\d\s]*")
REPORT_RE = re.compile(r"\b(q3 revenue|weekly ops)\b", re.I)


class RuleBasedPlanner:
    name = "rule-based"

    def plan(self, goal: str) -> list[PlanStep]:
        g = goal.strip()
        steps: list[PlanStep] = []
        report = REPORT_RE.search(g)
        if re.search(r"\b(send|email|share|forward)\b", g, re.I) and report:
            to = EMAIL_RE.search(g)
            name = report.group(1)
            steps.append({"tool": "get_report", "args": {"name": name}, "reason": "fetch report content"})
            steps.append({"tool": "send_report",
                          "args": {"recipient": to.group(0) if to else "unknown-recipient",
                                   "subject": f"{name.title()} report",
                                   "body": "{{steps[0].output}}"},
                          "reason": "deliver report (external side effect)"})
            return steps
        if report:
            return [{"tool": "get_report", "args": {"name": report.group(1)}, "reason": "fetch report"}]
        cust = CUST_RE.search(g)
        if cust and re.search(r"\b(look ?up|find|show|get|fetch|details?|profile)\b", g, re.I):
            cid = cust.group(1).upper()
            if "-" not in cid:
                cid = cid[0] + "-" + cid[1:]
            return [{"tool": "lookup_customer", "args": {"customer_id": cid}, "reason": "CRM lookup"}]
        m = MATH_RE.search(g)
        if m and re.search(r"\b(calc|calculate|compute|what is|what's|sum|total)\b|[-+*/]", g, re.I):
            return [{"tool": "calculator", "args": {"expression": m.group(0).strip()}, "reason": "arithmetic"}]
        return steps


SYSTEM_PROMPT = """You are the planning module of a safe agent. Output ONLY JSON:
{"steps":[{"tool":"<name>","args":{...},"reason":"..."}]}
Allowed tools and args:
- calculator(expression: str)
- lookup_customer(customer_id: str)  # ids look like C-1001
- get_report(name: str)  # 'Q3 revenue' or 'weekly ops'
- send_report(recipient: str, subject: str, body: str)  # body may be "{{steps[N].output}}"
Return {"steps":[]} if the goal cannot be done with these tools."""


class LLMPlanner:
    name = "llm"

    def __init__(self, api_key: str, base_url: str | None = None, model: str | None = None,
                 fallback: Planner | None = None, timeout: float = 30):
        self.api_key = api_key
        self.base_url = (base_url or os.getenv("LOG_ON_LLM_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.model = model or os.getenv("LOG_ON_LLM_MODEL", "gpt-4o-mini")
        self.fallback = fallback or RuleBasedPlanner()
        self.timeout = timeout
        self.last_error: str | None = None

    def _complete(self, goal: str) -> str:
        body = json.dumps({"model": self.model, "temperature": 0,
                           "response_format": {"type": "json_object"},
                           "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                        {"role": "user", "content": goal}]}).encode()
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {self.api_key}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())["choices"][0]["message"]["content"]

    @staticmethod
    def parse(raw: str) -> list[PlanStep]:
        data = json.loads(raw)
        steps = data.get("steps", [])
        if not isinstance(steps, list):
            raise ValueError("steps must be a list")
        out: list[PlanStep] = []
        for s in steps:
            if s.get("tool") not in TOOL_POLICY or not isinstance(s.get("args", {}), dict):
                raise ValueError(f"invalid step {s!r}")
            out.append({"tool": s["tool"], "args": s.get("args", {}), "reason": str(s.get("reason", ""))})
        return out

    def plan(self, goal: str) -> list[PlanStep]:
        try:
            return self.parse(self._complete(goal))
        except Exception as exc:  # noqa: BLE001 - any LLM failure falls back to deterministic rules
            self.last_error = f"{type(exc).__name__}: {exc}"
            return self.fallback.plan(goal)


def get_planner() -> Planner:
    key = os.getenv("LOG_ON_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    return LLMPlanner(key) if key else RuleBasedPlanner()
