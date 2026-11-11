"""Phase 4: DSPy programs, grader-based metric, optimisation pipeline (DummyLM) and the human-approval gate."""
import json

import dspy
import pytest
from dspy.utils import DummyLM

from log_on.learning.metrics import parse_steps, plan_metric, score_plan
from log_on.learning.planner import DSPyPlanner, DSPyVerifier
from log_on.learning.registry import PromotionRefused, PromptRegistry, pii_findings
from log_on.learning.signatures import TOOLS_DOC, PlannerProgram

CALC = [{"tool": "calculator", "args": {"expression": "1250 * 0.075 + 40"}}]
SEND = [{"tool": "get_report", "args": {"name": "q3 revenue"}},
        {"tool": "send_report", "args": {"recipient": "finance@acme.example", "subject": "Q3", "body": "{{steps[0].output}}"}}]


def test_plan_metric_mirrors_the_trajectory_grader():
    assert score_plan(CALC, [{"tool": "calculator", "args": {"expression": "1250*0.075+40"}}])["score"] == 1.0
    assert score_plan(CALC, [{"tool": "calculator", "args": {"expression": "1250*0.075"}}])["score"] == 0.6  # args wrong
    assert score_plan(SEND, list(reversed(SEND)))["score"] < 1.0                                        # order matters
    gated = score_plan(CALC, CALC + [{"tool": "send_report", "args": {"recipient": "x@evil.example"}}])
    assert gated["score"] == 0.0 and "approval-gated" in gated["reason"]                              # safety gate
    assert score_plan(CALC, None)["score"] == 0.0
    assert score_plan([], [])["score"] == 1.0                                                          # "no tool fits"
    assert parse_steps("[{'tool': 'calculator', 'args': {'expression': '2+2'}}]")[0]["tool"] == "calculator"
    assert parse_steps('[{"tool": "rm_rf", "args": {}}]') is None                                   # allowlist


def _ex(goal, steps):
    return dspy.Example(goal=goal, context="{}", tools=TOOLS_DOC, gold_steps=json.dumps(steps),
                        steps_json=json.dumps(steps), id=goal).with_inputs("goal", "context", "tools")


def test_bootstrap_compiles_demos_with_dummy_lm(tmp_path):
    from log_on.learning.optimize import compile_program, evaluate
    train = [_ex("Calculate 1250 * 0.075 + 40", CALC), _ex("Send the Q3 revenue report to finance@acme.example", SEND)]
    answers = {"Calculate 1250": {"steps_json": json.dumps(CALC)},
               "Send the Q3": {"steps_json": json.dumps(SEND)}}
    lm = DummyLM(answers)
    compiled = compile_program(PlannerProgram(), train, lm, "bootstrap", max_demos=2)
    assert len(compiled.plan.demos) == 2
    assert evaluate(compiled, train, lm, threads=1)["mean"] == 1.0
    path = tmp_path / "p.json"
    compiled.save(str(path))
    again = PlannerProgram()
    again.load(str(path))
    assert len(again.plan.demos) == 2


def test_dspy_planner_validates_and_falls_back(tmp_path):
    ok = DSPyPlanner(lm=DummyLM([{"steps_json": json.dumps(CALC)}]), version="t")
    assert ok.plan("Calculate 1250 * 0.075 + 40") == [{**CALC[0], "reason": ""}] and ok.name == "dspy:planner@t"
    bad = DSPyPlanner(lm=DummyLM([{"steps_json": '[{"tool": "shell", "args": {"cmd": "id"}}]'}]))
    assert bad.plan("Calculate 2+2")[0]["tool"] == "calculator" and "invalid plan" in bad.last_error


def test_dspy_verifier_never_overrides_hard_rule_failures():
    v = DSPyVerifier(lm=DummyLM([{"passed": True, "reason": "fine"}]))
    bad = v.verify("Calculate 2+2", [{"tool": "calculator"}], [{"tool": "calculator", "ok": True, "output": "error: x"}], [])
    assert not bad["passed"] and "dspy not consulted" in bad["verifier"]
    good = v.verify("Calculate 2+2", [{"tool": "calculator", "args": {"expression": "2+2"}}],
                    [{"tool": "calculator", "ok": True, "output": "4", "args": {"expression": "2+2"}}],
                    [{"tool": "calculator", "output": "4"}])
    assert good["passed"] and good["verifier"].startswith("dspy:verifier")


def _propose(reg, tmp=None, **over):
    meta = {"model": "dummy", "optimizer": "bootstrap", "scores": {"baseline": 0.5, "optimized": 0.8},
            "eval_gate": {"passed": True, "failures": []}}
    meta.update(over)
    prog = PlannerProgram()
    prog.plan.demos = [_ex("Calculate 1250 * 0.075 + 40", CALC)]
    f = reg.root / "_tmp_program.json"
    prog.save(str(f))
    text = f.read_text()
    f.unlink()
    return reg.propose("planner", text, meta)


def test_registry_learning_proposes_human_approves(tmp_path):
    reg = PromptRegistry(tmp_path)
    v = _propose(reg)
    assert reg.active("planner") is None and reg.versions("planner")[v]["status"] == "proposed"
    for robot in ("learning", "auto", "", "ci"):
        with pytest.raises(PromotionRefused):
            reg.approve("planner", v, by=robot)
    reg.approve("planner", v, by="rhonda", note="reviewed demos")
    assert reg.active("planner") == v
    found = reg.load_active("planner")
    assert found and found[0] == v
    p = DSPyPlanner.from_registry(lm=DummyLM([{"steps_json": json.dumps(CALC)}]), registry=reg)
    assert p.name == f"dspy:planner@{v}" and p.plan("Calculate 1250 * 0.075 + 40")[0]["tool"] == "calculator"
    assert reg.log.verify()  # hash-chained approvals log
    assert reg.rollback("planner", by="rhonda") == v and reg.active("planner") is None


def test_registry_refuses_regressions_failed_gates_tampering_and_pii(tmp_path):
    reg = PromptRegistry(tmp_path)
    worse = _propose(reg, scores={"baseline": 0.8, "optimized": 0.7})
    with pytest.raises(PromotionRefused, match="below baseline"):
        reg.approve("planner", worse, by="rhonda")
    gate = _propose(reg, eval_gate={"passed": False, "failures": ["attack pass rate 0.9 < 1.0"]})
    with pytest.raises(PromotionRefused, match="eval gate"):
        reg.approve("planner", gate, by="rhonda")
    good = _propose(reg)
    f = tmp_path / reg.versions("planner")[good]["file"]
    f.write_text(f.read_text().replace("1250", "9999"))
    with pytest.raises(PromotionRefused, match="sha256"):
        reg.approve("planner", good, by="rhonda")
    good2 = _propose(reg)
    reg.approve("planner", good2, by="rhonda")
    f2 = tmp_path / reg.versions("planner")[good2]["file"]
    f2.write_text(f2.read_text() + " ")                       # swapped after approval
    p = DSPyPlanner.from_registry(lm=DummyLM([{"steps_json": "[]"}]), registry=reg)
    assert p.version == "refused" and p.plan("Calculate 2+2")[0]["tool"] == "calculator"  # rules, not the prompt
    with pytest.raises(PromotionRefused, match="personal data"):
        reg.propose("planner", '{"demo": "call 08031234567 or mail adaeze.okafor@example.com"}', {})
    assert pii_findings('{"to": "finance@acme.example", "nin": "12345678901"}') == []  # documented fakes allowed


def test_trace_dataset_keeps_only_trajectory_verified_plans(tmp_path):
    from log_on.evals.runner import load_cases
    from log_on.learning.dataset import augment, collect_traces, split
    cases = [c for c in load_cases() if c["id"] in ("N01", "N06", "N10", "E07", "E04", "A01", "A16")]
    t = collect_traces(cases, tmp_path)
    ids = {p["id"] for p in t["plans"]}
    assert {"N01", "N06", "N10", "E07"} <= ids and not ids & {"A01", "A16"}
    n06 = next(p for p in t["plans"] if p["id"] == "N06")
    assert n06["gold_steps"][1]["args"]["recipient"] == "finance@acme.example"
    n10 = next(p for p in t["plans"] if p["id"] == "N10")
    assert n10["context"]["entities"] and n10["gold_steps"][0]["args"]["entity_ids"] == ["CUS-005"]
    assert any(not v["passed"] for v in t["verifications"]) and any(v["passed"] for v in t["verifications"])
    aug = augment(12, graph_path=t["graph"])
    assert len(aug) == 12 and len({a["goal"] for a in aug}) == 12
    tr, dv = split(t["plans"] + aug)
    assert tr and dv and not {e["id"] for e in tr} & {e["id"] for e in dv}
    assert split(t["plans"] + aug) == (tr, dv)  # deterministic


def test_registry_verify_detects_tampering_and_broken_chain(tmp_path):
    reg = PromptRegistry(tmp_path)
    v = _propose(reg)
    assert reg.verify() == []
    f = tmp_path / reg.versions("planner")[v]["file"]
    f.write_text(f.read_text() + " ")
    assert any("sha256" in p for p in reg.verify())
    f.write_text(f.read_text()[:-1])
    lines = (tmp_path / "approvals.jsonl").read_text().splitlines()
    (tmp_path / "approvals.jsonl").write_text("\n".join([lines[0].replace("propose", "approve")] + lines[1:]) + "\n")
    assert any("chain" in p for p in reg.verify())
