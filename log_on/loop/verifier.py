"""Verification node logic: does the (validated) result actually satisfy the goal?

Loop Engineering idea: an agent loop is only as good as its *check*. Each attempt ends with a verifier
verdict; a failing verdict triggers a revised plan (bounded) and finally a human escalation.

RuleVerifier (default) is deterministic: tool errors, empty output, "couldn't answer" signals, and
goal-specific checks (calculator used every number in the goal, a requested send went to the address
in the goal, the requested customer id is in the lookup). LLMVerifier adds a model-graded check on top,
used only when an OpenAI-compatible key is configured. Rules stay authoritative: if they fail, the LLM
is not asked (it cannot "approve away" a hard failure).
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..planner import CUST_RE, EMAIL_RE
from ..signals import FAIL_SIGNALS

SEND_VERB = re.compile(r"\b(send|email|share|forward)\b", re.I)
CALC_VERB = re.compile(r"\b(calc|calculate|compute|what is|what's|sum|total)\b", re.I)
NUM = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text: str) -> list[str]:
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)  # 1,250 -> 1250
    return [n.rstrip("0").rstrip(".") if "." in n else n for n in NUM.findall(text)]


class RuleVerifier:
    name = "rule-based"

    def __init__(self):
        self.last_usage: dict | None = None

    def verify(self, goal: str, plan: list[dict], tool_results: list[dict], validated: list[dict]) -> dict:
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"name": name, "passed": bool(ok), "detail": detail})

        tools = [r.get("tool") for r in tool_results]
        check("executed", len(tool_results) >= len(plan) and bool(tool_results),
              f"{len(tool_results)}/{len(plan)} planned steps returned a result")
        for i, r in enumerate(tool_results):
            out = str(r.get("output") or "")
            bad = FAIL_SIGNALS.search(out)
            check(f"step{i}:{r.get('tool')}:ok", bool(r.get("ok", True)) and not bad and out.strip() != "",
                  f"signal {bad.group(0).strip()!r}" if bad else ("empty output" if not out.strip() else "ok"))

        if "calculator" in tools:
            want = set(_numbers(goal))
            for r in tool_results:
                if r.get("tool") != "calculator":
                    continue
                expr = str((r.get("args") or {}).get("expression", ""))
                missing = sorted(want - set(_numbers(expr)))
                check("calculator:operands", not missing,
                      f"expression {expr!r} ignores {missing} from the goal" if missing else "all goal numbers used")

        to = EMAIL_RE.search(goal)
        if to and SEND_VERB.search(goal):
            sent = [r for r in tool_results if r.get("tool") == "send_report" and r.get("ok", True)
                    and (r.get("args") or {}).get("recipient", "").lower() == to.group(0).lower()
                    and not FAIL_SIGNALS.search(str(r.get("output") or ""))]
            check("goal:delivered", bool(sent),
                  "send_report reached the requested recipient" if sent else
                  "goal asks to send something, but nothing was sent to the requested recipient")

        cust = CUST_RE.search(goal)
        if cust and "lookup_customer" in tools:
            cid = cust.group(1).upper().replace("C", "C-", 1) if "-" not in cust.group(1) else cust.group(1).upper()
            hit = any(r.get("tool") == "lookup_customer" and cid in str(r.get("output")) for r in tool_results)
            check("goal:entity", hit, f"lookup returned {cid}" if hit else f"lookup did not return {cid}")

        passed = all(c["passed"] for c in checks)
        return {"verifier": self.name, "passed": passed,
                "score": round(sum(c["passed"] for c in checks) / len(checks), 3) if checks else 0.0,
                "checks": checks, "reasons": [f"{c['name']}: {c['detail']}" for c in checks if not c["passed"]]}


LLM_SYSTEM = """You verify whether an agent's tool results satisfy the user's goal. Output ONLY JSON:
{"passed": true|false, "reasons": ["..."]}
Fail if the results are off-topic, incomplete, contradict the goal, or only partially answer it.
Personal data in the results has been redacted as <EMAIL>, <PHONE>, <NIN>, <CARD>; that is expected, not a failure."""


class LLMVerifier(RuleVerifier):
    name = "llm+rules"

    def __init__(self, api_key: str, base_url: str, model: str):
        super().__init__()
        self.settings = {"api_key": api_key, "base_url": base_url, "model": model}
        self.last_error: str | None = None

    def verify(self, goal, plan, tool_results, validated):
        verdict = super().verify(goal, plan, tool_results, validated)
        self.last_usage, self.last_error = None, None
        if not verdict["passed"]:
            return {**verdict, "verifier": "rule-based (llm not consulted: hard checks failed)"}
        from ..llm import chat_json
        user = json.dumps({"goal": goal, "plan": plan, "validated_results": validated}, default=str)[:6000]
        try:
            data, self.last_usage = chat_json(LLM_SYSTEM, user, **self.settings)
            ok = bool(data.get("passed"))
            reasons = [str(r) for r in data.get("reasons", [])][:5]
        except Exception as exc:  # noqa: BLE001 - an unavailable model must not fail the run; rules stand
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {**verdict, "verifier": f"rule-based (llm failed: {self.last_error})"}
        verdict["checks"].append({"name": "llm:goal_satisfied", "passed": ok, "detail": "; ".join(reasons) or "ok"})
        verdict["passed"] = ok
        verdict["score"] = round(sum(c["passed"] for c in verdict["checks"]) / len(verdict["checks"]), 3)
        verdict["reasons"] = [f"{c['name']}: {c['detail']}" for c in verdict["checks"] if not c["passed"]]
        return {**verdict, "verifier": self.name}


def get_verifier():
    from ..llm import llm_settings
    s = llm_settings()
    return LLMVerifier(**s) if s else RuleVerifier()
