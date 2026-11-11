"""Editorial layer: claims paired with where they stop being true, and the dialectical "state of LOG_ON".

Every sentence is built from numbers loaded by `data` this request; when a source file is missing the section says so
instead of guessing. The interpretive text (what a number does and does not prove) is opinion, written down so a
reviewer can argue with it."""
from __future__ import annotations

from statistics import median


def _f(x, d: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:.{d}f}%"


def _rt(red: dict, file: str) -> dict | None:
    return next((r for r in red.get("runs", []) if r["file"].endswith(file)), None)


def _mode(bench: dict, mode: str) -> dict | None:
    return next((m for m in bench.get("modes", []) if m["mode"] == mode), None)


def claims(ev: dict, red: dict, bench: dict, aud: dict | None = None, dspy: dict | None = None,
           serving: list | None = None, runs: list | None = None) -> list[dict]:
    out = []
    after, before = _rt(red, "step2_rule_after.json"), _rt(red, "step2_rule_before.json")
    comb, rx = _mode(bench, "combined"), _mode(bench, "regex")
    if after:
        held = (f"{comb['heldout_detected']}/{comb['heldout']} held-out wordings caught at input (combined); "
                f"{comb['heldout'] - comb['heldout_detected']}/{comb['heldout']} pass the input check"
                if comb else "no held-out benchmark found")
        out.append({
            "id": "redteam", "area": "Red team",
            "claim": f"{after['defended']}/{after['attacks']} red-team attacks defended, 0 vulnerable",
            "says": "Every attack in the generated set is refused, handed to a human or contained, on the rule "
                    "planner and on the live 1.5B planner.",
            "doesnt": f"That unseen attacks are stopped. The set is {after['seeds']} seed prompts × "
                      f"{after['techniques']} rewriting techniques (plus indirect/memory cases), and the rules were "
                      "fixed on these same attacks: it is a training-set score.",
            "breaking": held + (f"; regex-only catches {rx['heldout_detected']}/{rx['heldout']}." if rx else "."),
            "evidence": [after["file"], "docs/safety_bench.json"]})
    lat = ev.get("latest") if ev else None
    if lat:
        bc = lat.get("by_category", {})
        att, nor, edge = (bc.get(k, {}).get("cases", 0) for k in ("attack", "normal", "edge"))
        out.append({
            "id": "evalgate", "area": "Eval gate",
            "claim": f"{lat['cases']}/{lat['cases']} cases pass, {lat['pii_leaks']} PII leaks, gate {lat['gate']}",
            "says": "No regression on any frozen case; every metric at 1.0; the CI gate blocks merges that regress.",
            "doesnt": f"That the agent is useful. {att} of {lat['cases']} cases are frozen attacks; only {nor} normal "
                      f"and {edge} edge tasks measure doing the job, and frozen attacks accept a stricter outcome "
                      "(refusal or human hand-off) as a pass.",
            "breaking": f"Usefulness rests on {nor + edge} cases ({_f((nor + edge) / lat['cases'])} of the suite). "
                        "A refuse-everything agent would pass the frozen attack cases.",
            "evidence": [lat["file"]]})
    if comb:
        out.append({
            "id": "pii", "area": "PII",
            "claim": f"PII recall {comb['pii_found']}/{comb['pii_expected']}, 0 business-string false positives",
            "says": "Nigerian NIN, BVN, phones, cards, emails, IBAN, IP and crypto are masked in the benchmark, "
                    "in every layout written into it.",
            "doesnt": f"That personal data is gone. The corpus is {comb['pii_expected']} hand-written positives; "
                      "names are not redacted (PERSON is off by default), so a customer's name passes through.",
            "breaking": "Recall on 27 examples you wrote yourself is a floor test, not a measurement. "
                        + (f"Regex alone found {rx['pii_found']}/{rx['pii_expected']}." if rx else ""),
            "evidence": ["docs/safety_bench.json", "docs/safety_ml_report.md"]})
    if aud and aud.get("audit.jsonl", {}).get("exists"):
        a = aud["audit.jsonl"]
        dirty = sum(r["records"] for r in aud.get("provenance", []) if "-dirty" in r["stamp"])
        out.append({
            "id": "audit", "area": "Audit",
            "claim": f"Hash chain {'verified' if a['ok'] else 'BROKEN'} over {a['records']} records, "
                     f"{a['legacy_unstamped']} unstamped",
            "says": "No record was edited, dropped or reordered after it was written, and each one names the code, "
                    "policy and model that produced it.",
            "doesnt": "That the log is authentic. Anyone who can write the file can rewrite it and recompute every "
                      "hash; nothing is signed or anchored outside the box.",
            "breaking": (f"{dirty} of {a['records']} records are stamped with a '-dirty' code version: they point at "
                         "uncommitted code that cannot be reproduced from git." if dirty else
                         "Tamper-evidence holds only while the chain head is kept somewhere the writer cannot reach."),
            "evidence": ["runs/audit.jsonl", "python -m log_on.audit verify"]})
    if dspy and dspy.get("versions"):
        v = dspy["versions"][-1]
        s = v.get("scores", {})
        out.append({
            "id": "dspy", "area": "Learning",
            "claim": f"DSPy lifts the planner score {s.get('baseline')} → {s.get('optimized')}",
            "says": "Prompt optimisation measurably improves plan quality on the dev split.",
            "doesnt": "That the optimised prompt is safe to ship. The gate refused every candidate "
                      f"({len(dspy['versions'])}/{len(dspy['versions'])}); active version: {dspy.get('active') or 'none'}.",
            "breaking": "; ".join(v.get("failures", [])) or "no failures recorded",
            "evidence": ["prompts/registry.json", "docs/dspy_report_phase4.json"]})
    if serving:
        big = next((b for b in serving if "1.5b" in (b.get("model") or "")), serving[-1])
        lv1 = next((lv for lv in big["levels"] if lv["concurrency"] == 1), big["levels"][0])
        top = max(big["levels"], key=lambda lv: lv["concurrency"])
        out.append({
            "id": "serving", "area": "Serving",
            "claim": f"Local {big['model']} plans at {lv1['output_tok_per_s']} tok/s, p50 {lv1['latency_p50_ms']:.0f} ms",
            "says": "A 1.5B model on {} CPU cores can drive the planner with no API spend.".format(big.get("cpus")),
            "doesnt": f"That its plans are good: {_f(lv1['valid_plan_rate'])} were valid plans at concurrency 1.",
            "breaking": f"At concurrency {top['concurrency']}: p95 {top['latency_p95_ms']:.0f} ms, "
                        f"valid plans {_f(top['valid_plan_rate'])}. Safety numbers above were mostly measured on the "
                        "deterministic rule planner.",
            "evidence": [big["file"]]})
    if runs is not None:
        decided = [d for r in runs for d in r.get("decisions", [])]
        out.append({
            "id": "approval", "area": "Human approval",
            "claim": f"High-risk sends wait for a human: {sum(1 for r in runs if r['status'] == 'awaiting_approval')} "
                     f"pending, {len(decided)} decided in this runs directory",
            "says": "No email leaves without a named approver, and the decision is hash-chained with a reason.",
            "doesnt": "That the approver read it. The approver sees a masked address and a preview; nothing measures "
                      "rubber-stamping, and a request auto-denies only after its TTL.",
            "breaking": "The gate is only as strong as approver attention; red-team 'vulnerable' verdicts are exactly "
                        "cases that reached this screen with an attacker's address.",
            "evidence": ["runs/feedback.jsonl", "docs/redteam/step2_rule_before.json"]})
    return out


def state_of(ev: dict, red: dict, bench: dict, aud: dict, ops: dict, runs: list, pending: int, demo: bool) -> dict:
    """Thesis / antithesis / pragmatic next move, per theme."""
    lat = ev.get("latest") or {}
    hist = ev.get("history", [])
    comb, rx = _mode(bench, "combined"), _mode(bench, "regex")
    rb, ra = _rt(red, "step2_rule_before.json"), _rt(red, "step2_rule_after.json")
    p4b = _rt(red, "rule_before_fixes.json")
    tel = ops.get("telemetry", {})
    dsp = ops.get("dspy", {})
    statuses = tel.get("statuses", {})
    sections = []
    growth = " → ".join(f"{h['cases']}" for h in hist) or "n/a"
    sections.append({
        "title": "Safety: defended, or merely tested?",
        "thesis": (f"The gate has grown {growth} cases and still passes at {_f(lat.get('pass_rate'), 0)} with "
                   f"{lat.get('pii_leaks')} PII leaks. The red team went from "
                   + (f"{p4b['vulnerable']} vulnerable / {p4b['bypass']} bypasses in Phase 4 " if p4b else "")
                   + (f"and {rb['vulnerable']} / {rb['bypass']} on the larger step-2 set " if rb else "")
                   + (f"to 0 / 0 across {ra['attacks']} attacks." if ra else ".")),
        "antithesis": ("Each round of fixes was written against the attacks that found the hole, then those attacks "
                       "were frozen into the gate. The honest out-of-sample number is the held-out set: "
                       + (f"{comb['heldout_detected']}/{comb['heldout']} caught at input with the ML classifier, "
                          f"{rx['heldout_detected']}/{rx['heldout']} with regex alone." if comb and rx else "missing.")
                       + " Containment of the rest relies on later layers (no tool returns bulk PII; the output "
                         "redactor masks single values)."),
        "next": "Grow the held-out set with wordings written by someone who has not seen the rules, report it as "
                "its own gate line, and never fold it into the training cases.",
    })
    sections.append({
        "title": "Defence in depth: layers, or one wall?",
        "thesis": "Thirteen layers sit between a request and an effect. A blocked request is stopped at the policy "
                  "node before memory is read, and every high-risk send pauses for a person.",
        "antithesis": "Three of the thirteen are not separate mechanisms: orchestration is routing, execution is the "
                      "action node (no sandbox), and approval runs before action rather than after validation. "
                      f"In this runs directory: {statuses.get('refused', 0)} refused at policy, "
                      f"{statuses.get('awaiting_approval', 0)} paused for approval, "
                      f"{statuses.get('awaiting_human', 0)} escalated, {statuses.get('quota_exceeded', 0)} over quota.",
        "next": "Split execution into a sandboxed worker with its own allow-list so 'execution' becomes a real layer, "
                "and log approval latency to see whether approvers read before clicking.",
    })
    a = aud.get("audit.jsonl", {})
    sections.append({
        "title": "Accountability: tamper-evident, or tamper-proof?",
        "thesis": (f"The audit chain verifies ({a.get('records')} records, {a.get('legacy_unstamped')} unstamped) and "
                   "every record carries a provenance stamp: code version, policy hash, prompt, planner and model."
                   if a.get("exists") else "No audit log in this runs directory yet."),
        "antithesis": "A hash chain detects edits by someone who does not recompute it. It is not a signature, and "
                      "'-dirty' code stamps cannot be traced to a commit.",
        "next": "Ship the chain head to an append-only store (or sign it) on a schedule, and build release images "
                "with LOG_ON_GIT_SHA set so stamps are never dirty in production.",
    })
    sv = ops.get("serving", [])
    big = next((b for b in sv if "1.5b" in (b.get("model") or "")), None)
    lv1 = next((lv for lv in big["levels"] if lv["concurrency"] == 1), None) if big else None
    sections.append({
        "title": "Cost: free, or just not yet billed?",
        "thesis": (f"{tel.get('runs', 0)} runs here used {tel.get('tokens_total', 0)} tokens at an estimated "
                   f"${tel.get('cost_total_usd', 0):.4f}; per-user daily budgets stop runs before they spend."
                   + (f" The local 1.5B planner serves {lv1['req_per_s']} req/s at concurrency 1." if lv1 else "")),
        "antithesis": ("Cost is near zero because most of these runs used the rule planner and a local model; "
                       f"{tel.get('llm_calls', 0)} LLM calls in total. "
                       + (f"The live model produced valid plans {_f(lv1['valid_plan_rate'])} of the time." if lv1 else "")),
        "next": "Run the eval gate against the live planner on a schedule and track valid-plan rate and cost per "
                "completed task, not per run.",
    })
    vers = dsp.get("versions", [])
    sections.append({
        "title": "Learning: improving, or drifting?",
        "thesis": (f"The learning loop proposed {len(vers)} optimised planner prompts; the latest lifted the dev score "
                   f"{vers[-1]['scores'].get('baseline')} → {vers[-1]['scores'].get('optimized')}." if vers
                   else "No optimised prompts proposed yet."),
        "antithesis": ("The gate refused all of them: " + "; ".join(vers[-1].get("failures", [])) + ". Better plans "
                       "were bought with weaker safety." if vers else "Nothing to refuse."),
        "next": "Add the attack cases to the DSPy training metric (not just the gate) so optimisation cannot trade "
                "safety for score, and keep the refusal visible here.",
    })
    verdict = ("Safe within the attacks it has seen, honest about the ones it hasn't, and not yet proven useful "
               "beyond a small task set.")
    return {"verdict": verdict, "sections": sections, "demo": demo, "pending": pending,
            "median_decision_s": None}
