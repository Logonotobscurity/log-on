"""Phase 4: model serving layer (OpenAI-compatible providers, fake server, benchmark)."""
import os

import pytest

from log_on.graph import Orchestrator
from log_on.loop import LoopConfig, LLMVerifier
from log_on.planner import LLMPlanner, RuleBasedPlanner, get_planner
from log_on.serving import PRESETS, ProviderError, get_provider, make_provider, parse_json_lenient
from log_on.serving.bench import run_level, valid_plan
from log_on.serving.fake_server import FakeServer


@pytest.fixture
def fake():
    with FakeServer() as srv:
        yield srv


def test_provider_selection_from_env():
    assert get_provider({}) is None                                   # nothing configured -> rules everywhere
    assert get_provider({"LOG_ON_LLM_PROVIDER": "none", "LOG_ON_LLM_API_KEY": "k"}) is None
    p = get_provider({"LOG_ON_LLM_PROVIDER": "llamacpp"})              # local presets need no key
    assert p.config.base_url == PRESETS["llamacpp"]["base_url"] and p.model == PRESETS["llamacpp"]["model"]
    assert get_provider({"LOG_ON_LLM_PROVIDER": "vllm", "LOG_ON_LLM_MODEL": "m"}).model == "m"
    assert get_provider({"LOG_ON_LLM_PROVIDER": "openai"}) is None    # hosted preset without a key
    legacy = get_provider({"OPENAI_API_KEY": "sk-test"})               # Phase 1-3 behaviour preserved
    assert legacy.name == "openai" and legacy.config.describe()["api_key"] == "***"
    assert "sk-test" not in repr(legacy) and "sk-test" not in str(legacy.config)
    with pytest.raises(ValueError):
        get_provider({"LOG_ON_LLM_PROVIDER": "nope"})


def test_parse_json_lenient_handles_small_model_habits():
    assert parse_json_lenient('```json\n{"steps": []}\n```') == {"steps": []}
    assert parse_json_lenient('Sure! Here it is: {"a": "x}y", "b": {"c": 1}} hope that helps') == {"a": "x}y", "b": {"c": 1}}
    with pytest.raises(ValueError):
        parse_json_lenient("no json here")


def test_fake_server_speaks_openai_and_reports_usage(fake):
    prov = make_provider("fake", base_url=fake.base_url)
    assert prov.health()
    data, usage = prov.chat_json("You are the planning module of a safe agent.", "Calculate 2+2")
    assert data["steps"][0]["tool"] == "calculator"
    assert usage["provider"] == "fake" and usage["prompt_tokens"] > 0 and not usage["estimated"]


def test_llm_planner_through_provider_and_cost_is_zero_when_self_hosted(fake, tmp_path):
    prov = make_provider("fake", base_url=fake.base_url)
    planner = LLMPlanner(provider=prov)
    o = Orchestrator(planner=planner, audit_path=tmp_path / "a.jsonl", outbox=tmp_path / "o.jsonl",
                     loop=LoopConfig(verifier=LLMVerifier(provider=prov)))
    r = o.start("Calculate 1250 * 0.075 + 40")
    assert r["status"] == "completed" and "133.75" in r["final_output"]
    assert r["planner"] == "llm:fake:log-on-fake" and r["verification"]["verifier"] == "llm+rules"
    assert r["usage"]["llm_calls"] == 2 and r["usage"]["est_cost_usd"] == 0.0
    assert fake.requests == 2


def test_provider_outage_and_garbage_fall_back_to_rules(tmp_path):
    with FakeServer(mode="error") as srv:
        prov = make_provider("fake", base_url=srv.base_url, retries=1)
        with pytest.raises(ProviderError):
            prov.complete("x", "y")
        assert srv.requests == 2                                       # one retry
        p = LLMPlanner(provider=prov)
        assert p.plan("Calculate 2+2")[0]["tool"] == "calculator" and "unavailable" in p.last_error
    with FakeServer(mode="garbage") as srv:
        p = LLMPlanner(provider=make_provider("fake", base_url=srv.base_url))
        assert p.plan("Get the Q3 revenue report")[0]["tool"] == "get_report" and p.last_error


def test_compromised_model_cannot_exfiltrate(tmp_path):
    """A model that returns an exfiltration plan is stopped by recipient provenance before any approval."""
    with FakeServer(mode="malicious") as srv:
        o = Orchestrator(planner=LLMPlanner(provider=make_provider("fake", base_url=srv.base_url)),
                         audit_path=tmp_path / "a.jsonl", outbox=tmp_path / "o.jsonl")
        r = o.start("Calculate 2+2")
    assert r["status"] == "refused" and "did not give" in r["final_output"]
    assert "pending_approval" not in r and not (tmp_path / "o.jsonl").exists()


def test_get_planner_uses_env_provider(monkeypatch, fake):
    monkeypatch.delenv("LOG_ON_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LOG_ON_LLM_PROVIDER", raising=False)
    assert isinstance(get_planner(), RuleBasedPlanner)
    monkeypatch.setenv("LOG_ON_LLM_PROVIDER", "fake")
    monkeypatch.setenv("LOG_ON_LLM_BASE_URL", fake.base_url)
    p = get_planner()
    assert isinstance(p, LLMPlanner) and p.plan("Look up customer C-1001")[0]["tool"] == "lookup_customer"


def test_benchmark_measures_batching_against_slot_limits():
    """The benchmark sees what continuous batching buys: 4 slots ~4x the req/s of 1 slot at concurrency 4."""
    with FakeServer(per_token_ms=15, slots=1) as one, FakeServer(per_token_ms=15, slots=4) as four:
        r1 = run_level(make_provider("fake", base_url=one.base_url), concurrency=4, n=8, max_tokens=64)
        r4 = run_level(make_provider("fake", base_url=four.base_url), concurrency=4, n=8, max_tokens=64)
    assert r1["ok"] == r4["ok"] == 8 and r1["errors"] == 0
    assert r4["req_per_s"] > 2 * r1["req_per_s"]
    assert r4["valid_plan_rate"] == 1.0 and four.max_inflight >= 2
    assert valid_plan('{"steps":[{"tool":"calculator","args":{"expression":"1+1"}}]}')
    assert not valid_plan('{"steps":[{"tool":"rm","args":{}}]}')


LIVE = os.getenv("LOG_ON_TEST_LIVE_LLM")  # e.g. http://127.0.0.1:8082/v1 (llama.cpp / vLLM / Ollama)


@pytest.mark.skipif(not LIVE, reason="set LOG_ON_TEST_LIVE_LLM=<base_url> to test a real local model server")
def test_live_local_model_plans_through_the_full_graph(tmp_path):
    prov = make_provider("llamacpp", base_url=LIVE, model=os.getenv("LOG_ON_TEST_LIVE_MODEL", "qwen2.5-1.5b-instruct"),
                         timeout=120)
    assert prov.health()
    o = Orchestrator(planner=LLMPlanner(provider=prov), audit_path=tmp_path / "a.jsonl", outbox=tmp_path / "o.jsonl")
    r = o.start("Calculate 1250 * 0.075 + 40")
    assert r["status"] == "completed" and "133.75" in r["final_output"]
