"""Phase 3: OpenTelemetry tracing, replay CLI and the token/cost ledger."""
import json

from log_on.graph import Orchestrator
from log_on.loop import LoopConfig
from log_on.planner import RuleBasedPlanner
from log_on.telemetry import Budget, RunTracer, add_usage, est_tokens, llm_cost
from log_on.telemetry.replay import list_runs, load_spans, main as trace_main, render, spans_for


def make(tmp_path, **kw):
    return Orchestrator(planner=RuleBasedPlanner(), audit_path=tmp_path / "audit.jsonl",
                        outbox=tmp_path / "outbox.jsonl", loop=LoopConfig(), **kw)


def test_every_node_and_tool_call_is_a_span(tmp_path):
    o = make(tmp_path)
    r = o.start("What is 2^10 + 5")
    spans = spans_for(load_spans(tmp_path / "traces.jsonl"), r["run_id"])
    names = [s["name"] for s in spans]
    root = next(s for s in spans if s["name"] == "run.start")
    assert root["parent_span_id"] is None and root["attributes"]["log_on.status"] == "completed"
    nodes = [s for s in spans if s["name"].startswith("node.")]
    assert all(s["parent_span_id"] == root["span_id"] for s in nodes)
    assert names.count("node.verification") == 2 and names.count("tool.calculator") == 2
    tools = [s for s in spans if s["name"] == "tool.calculator"]
    actions = {s["span_id"] for s in spans if s["name"] == "node.action"}
    assert all(t["parent_span_id"] in actions and t["duration_ms"] > 0 for t in tools)
    ver = [s for s in spans if s["name"] == "node.verification"]
    assert ver[0]["attributes"]["log_on.verify.passed"] == "False"
    routes = [(e["attributes"]["from"], e["attributes"]["to"]) for e in root["events"] if e["name"] == "route"]
    assert ("verification", "replan") in routes and ("replan", "permission") in routes
    assert root["attributes"]["log_on.usage.tool_calls"] == 2


def test_resume_is_a_second_trace_of_the_same_run(tmp_path):
    o = make(tmp_path)
    r = o.start("Send the Q3 revenue report to finance@acme.example")
    o.resume(r["run_id"], approved=True)
    all_spans = load_spans(tmp_path / "traces.jsonl")
    spans = spans_for(all_spans, r["run_id"])
    roots = [s for s in spans if s["parent_span_id"] is None]
    assert [s["name"] for s in roots] == ["run.start", "run.resume"], (
        f"{len(all_spans)} spans in file, {len(spans)} for run {r['run_id']}, exporters={o.tracer.exporters} "
        f"errors={o.tracer.errors} roots={[x['name'] for x in all_spans if x['parent_span_id'] is None]}")
    assert len({s["trace_id"] for s in roots}) == 2
    assert any(s["name"] == "tool.send_report" for s in spans)


def test_traces_never_contain_raw_pii(tmp_path):
    o = make(tmp_path)
    o.start("Look up customer C-1001")
    o.start("Remember my NIN is 12345678901 and my phone is 08000000001")
    raw = (tmp_path / "traces.jsonl").read_text()
    for s in ["adaeze.okafor@example.com", "08031234567", "12345678901", "4111 1111 1111 1111", "08000000001"]:
        assert s not in raw
    assert "<EMAIL>" in raw


def test_replay_cli_renders_tree_and_list(tmp_path, capsys):
    o = make(tmp_path)
    r = o.start("Look up customer C-9999")
    o.respond(r["run_id"], "abort")
    text = render(load_spans(tmp_path / "traces.jsonl"), r["run_id"][:6])
    assert "trace 1/2" in text and "trace 2/2" in text and "tool.lookup_customer" in text
    assert "decision.escalation=abort" in text and "latency by tool" in text
    assert trace_main(["--file", str(tmp_path / "traces.jsonl"), "--list"]) == 0
    assert r["run_id"] in capsys.readouterr().out
    runs = list_runs(load_spans(tmp_path / "traces.jsonl"))
    assert runs[0]["invocations"] == 2 and runs[0]["status"] == "escalated"


def test_tracing_can_be_disabled_and_bad_exporters_never_break_a_run(tmp_path, monkeypatch):
    o = make(tmp_path, tracer=False)
    assert o.start("Calculate 2 * 21")["status"] == "completed"
    assert not (tmp_path / "traces.jsonl").exists()
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    t = RunTracer(tmp_path / "t.jsonl", exporters=["jsonl", "langfuse", "console"])
    assert t.exporters == ["jsonl", "console"] and "langfuse" in t.errors[0]


def test_otlp_and_langfuse_exporters_configure_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:3000")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LOG_ON_TRACE_EXPORTERS", "otlp,langfuse")
    t = RunTracer(tmp_path / "t.jsonl")
    assert t.exporters == ["otlp", "langfuse"] and not t.errors


def test_cost_ledger_and_budget_math():
    assert est_tokens("abcd" * 10) == 10 and est_tokens("") == 0
    assert llm_cost("gpt-4o-mini", 1_000_000, 1_000_000) == 0.75
    u = add_usage(None, steps=1, llm={"model": "gpt-4o-mini", "prompt_tokens": 1000, "completion_tokens": 100})
    u = add_usage(u, steps=1, tool_calls=2, context_tokens=50)
    assert u["steps"] == 2 and u["llm_calls"] == 1 and u["total_tokens"] == 1150 and u["est_cost_usd"] == 0.00021
    b = Budget(max_steps=10, max_tool_calls=2, max_tokens=1000, max_cost_usd=0.0001)
    assert set(map(lambda s: s.split()[0], b.exceeded(u, pending_tool_calls=1))) == {"tool", "tokens", "cost"}


def test_llm_planner_usage_is_costed(tmp_path):
    class FakeLLMPlanner(RuleBasedPlanner):
        name = "llm"

        def plan(self, goal, context=None):
            self.last_usage = {"model": "gpt-4o-mini", "calls": 1, "prompt_tokens": 800, "completion_tokens": 60}
            return super().plan(goal, context)
    o = Orchestrator(planner=FakeLLMPlanner(), audit_path=tmp_path / "a.jsonl", outbox=tmp_path / "o.jsonl",
                     loop=LoopConfig())
    r = o.start("Calculate 2 * 21")
    assert r["usage"]["prompt_tokens"] == 800 and r["usage"]["est_cost_usd"] > 0
    audit_usage = [x["data"]["usage"] for x in o.audit.read(r["run_id"]) if x["node"] == "planning"][0]
    assert audit_usage["llm_calls"] == 1
    spans = load_spans(tmp_path / "traces.jsonl")
    assert json.dumps(spans).count("usage.est_cost_usd") > 3
