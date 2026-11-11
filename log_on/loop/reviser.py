"""Plan revision for the self-checking loop.

Given the goal, the failed plan and the verifier's reasons, propose a *different* plan, or [] when no
safe repair is known (which escalates to a human). Safety invariants, enforced here and tested:

* a revised plan never adds a tool that needs approval (e.g. send_report) unless the failed plan already
  had it, so a loop can never "discover" a new externally visible side effect;
* a plan that was already tried is never proposed again (no spinning on the same failure);
* every revised plan goes back through permission -> (approval) -> action, so approvals are re-asked.

RuleReviser is deterministic. With an LLMPlanner, the planner is re-asked with the verifier feedback
first (Loop Engineering: "feed the check back into the next attempt"), falling back to the rules.
"""
from __future__ import annotations

import difflib
import json
import re

from ..permissions import TOOL_POLICY
from ..planner import KNOWN_REPORTS, MATH_RE, PATH_RE


def plan_key(plan: list[dict]) -> str:
    return json.dumps([[s.get("tool"), s.get("args", {})] for s in plan], sort_keys=True, default=str)


def normalise_math(text: str) -> str:
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)            # 1,250 -> 1250
    t = re.sub(r"(?<=\d)\s*(?:x|X|×)\s*(?=\d)", " * ", t)   # 12 x 4 -> 12 * 4
    t = t.replace("÷", "/").replace("^", "**")
    return t


def _failed_steps(verdict: dict) -> set[int]:
    out = set()
    for c in verdict.get("checks", []):
        m = re.match(r"step(\d+):", c["name"])
        if m and not c["passed"]:
            out.add(int(m.group(1)))
    return out


class RuleReviser:
    name = "rule-based"

    def __init__(self):
        self.last_usage: dict | None = None
        self.last_error: str | None = None

    def repair(self, goal: str, plan: list[dict], verdict: dict, tool_results: list[dict],
               context: dict | None) -> tuple[list[dict], list[str]]:
        notes: list[str] = []
        failed = _failed_steps(verdict)
        names = {c["name"] for c in verdict.get("checks", []) if not c["passed"]}
        new: list[dict] = []
        for i, step in enumerate(plan):
            tool, args = step.get("tool"), dict(step.get("args") or {})
            out = str(tool_results[i].get("output", "")) if i < len(tool_results) else ""
            if tool == "calculator" and (i in failed or "calculator:operands" in names):
                m = MATH_RE.search(normalise_math(goal))
                if m and m.group(0).strip() != args.get("expression"):
                    new.append({"tool": "calculator", "args": {"expression": m.group(0).strip()},
                                "reason": "revised: normalised the goal's arithmetic (separators, x, ^)"})
                    notes.append(f"calculator expression {args.get('expression')!r} -> {m.group(0).strip()!r}")
                    continue
            if tool == "get_report" and i in failed and "No report named" in out:
                match = difflib.get_close_matches(str(args.get("name", "")).lower(), KNOWN_REPORTS, n=1, cutoff=0.5)
                if match:
                    new.append({"tool": "get_report", "args": {"name": match[0]},
                                "reason": f"revised: closest known report to {args.get('name')!r}"})
                    notes.append(f"report name {args.get('name')!r} -> {match[0]!r}")
                    continue
            if tool == "graph_query" and i in failed:
                ents = [e for e in (context or {}).get("entities", []) if e.get("from") == "goal"]
                if PATH_RE.search(goal) and len(ents) >= 2:
                    new.append({"tool": "find_path", "args": {"source": ents[0]["id"], "target": ents[1]["id"],
                                                              "max_hops": 5}, "reason": "revised: path search"})
                    notes.append("graph_query could not answer -> find_path between linked entities")
                    continue
                if ents:
                    new.append({"tool": "entity_lookup", "args": {"name": ents[0]["name"]},
                                "reason": "revised: template miss, fall back to the linked entity's profile"})
                    notes.append(f"graph_query could not answer -> entity_lookup({ents[0]['name']!r})")
                    continue
            if tool == "find_path" and i in failed and "No path within" in out:
                hops = int(args.get("max_hops", 5))
                if hops < 8:
                    new.append({**step, "args": {**args, "max_hops": 8}, "reason": "revised: widen path search"})
                    notes.append("find_path widened to 8 hops")
                    continue
            new.append(step)
        if plan_key(new) == plan_key(plan):
            return [], notes or ["no known safe repair for: " + "; ".join(verdict.get("reasons", [])[:3])]
        return new, notes

    def revise(self, goal: str, plan: list[dict], verdict: dict, tool_results: list[dict],
               context: dict | None, tried: list[str]) -> tuple[list[dict], list[str]]:
        self.last_usage = None
        new, notes = self.repair(goal, plan, verdict, tool_results, context)
        return self._safe(new, plan, tried, notes)

    @staticmethod
    def _safe(new: list[dict], plan: list[dict], tried: list[str], notes: list[str]) -> tuple[list[dict], list[str]]:
        if not new:
            return [], notes
        had = {s.get("tool") for s in plan}
        added_risky = [s["tool"] for s in new if TOOL_POLICY.get(s.get("tool"), {}).get("approval")
                       and s["tool"] not in had]
        if added_risky:
            return [], notes + [f"rejected revision: it adds approval-gated tools {added_risky}"]
        if plan_key(new) in tried:
            return [], notes + ["rejected revision: that plan was already tried"]
        return new, notes


class PlannerReviser(RuleReviser):
    """Re-ask an LLM planner with the verifier's feedback, then fall back to the rule repairs."""
    name = "llm+rules"

    def __init__(self, planner):
        super().__init__()
        self.planner = planner

    def revise(self, goal, plan, verdict, tool_results, context, tried):
        self.last_usage, self.last_error = None, None
        ctx = {**(context or {}), "feedback": verdict.get("reasons", []),
               "failed_plans": [json.loads(k) for k in tried][-3:]}
        try:
            new = self.planner.plan(goal, ctx)
            self.last_usage = getattr(self.planner, "last_usage", None)
            self.last_error = getattr(self.planner, "last_error", None)
            safe, notes = self._safe(new, plan, tried, ["llm planner re-asked with verifier feedback"])
            if safe and not self.last_error:
                return safe, notes
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
        new, notes = self.repair(goal, plan, verdict, tool_results, context)
        return self._safe(new, plan, tried, notes)


def get_reviser(planner):
    return PlannerReviser(planner) if hasattr(planner, "_complete") else RuleReviser()
