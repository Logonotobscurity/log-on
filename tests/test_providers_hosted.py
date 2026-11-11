"""Hosted providers (Groq, OpenRouter): presets, key isolation, headers, 429 handling, pacing, per-provider cost,
and the provider comparison report. Everything runs against the local fake server: no API keys, no network."""
import json
import time
from email.utils import formatdate

import pytest

from log_on.redteam.attacks import generate
from log_on.serving import PRESETS, RateLimitError, get_provider, make_provider
from log_on.serving.bench import stream_once
from log_on.serving.compare import main as compare_main
from log_on.serving.compare import parse_target, stratified_sample
from log_on.serving.fake_server import FakeServer
from log_on.serving.providers import OPENROUTER_HEADERS, retry_after_s
from log_on.telemetry.cost import add_usage, llm_cost, price_for

FAKE_GROQ = "gsk_FAKE_TEST_KEY_not_real"
FAKE_OR = "sk-or-FAKE_TEST_KEY_not_real"
NO_PACING = {"LOG_ON_LLM_RPM": "0"}


def test_hosted_presets():
    assert PRESETS["groq"]["base_url"] == "https://api.groq.com/openai/v1"
    assert PRESETS["openrouter"]["base_url"] == "https://openrouter.ai/api/v1"
    assert PRESETS["groq"]["key_env"] == PRESETS["groq-qwen"]["key_env"] == "GROQ_API_KEY"
    assert PRESETS["openrouter"]["key_env"] == PRESETS["openrouter-free"]["key_env"] == "OPENROUTER_API_KEY"
    assert PRESETS["openrouter"]["model"] == "qwen/qwen-2.5-72b-instruct"
    assert PRESETS["openrouter-free"]["model"].endswith(":free")
    assert "qwen" in PRESETS["groq-qwen"]["model"]
    assert OPENROUTER_HEADERS["HTTP-Referer"].startswith("https://") and OPENROUTER_HEADERS["X-Title"] == "LOG_ON"


def test_keys_only_go_to_their_own_provider():
    assert get_provider({"LOG_ON_LLM_PROVIDER": "groq"}) is None
    assert get_provider({"LOG_ON_LLM_PROVIDER": "groq", "OPENROUTER_API_KEY": FAKE_OR}) is None
    assert get_provider({"LOG_ON_LLM_PROVIDER": "groq", "OPENAI_API_KEY": "sk-x"}) is None
    assert get_provider({"LOG_ON_LLM_PROVIDER": "openrouter", "GROQ_API_KEY": FAKE_GROQ}) is None
    g = get_provider({"LOG_ON_LLM_PROVIDER": "groq", "GROQ_API_KEY": FAKE_GROQ, "OPENROUTER_API_KEY": FAKE_OR})
    assert g.config.api_key == FAKE_GROQ and g.model == PRESETS["groq"]["model"]
    o = get_provider({"LOG_ON_LLM_PROVIDER": "openrouter-free", "GROQ_API_KEY": FAKE_GROQ, "OPENROUTER_API_KEY": FAKE_OR})
    assert o.config.api_key == FAKE_OR
    # an explicit generic key still works for any provider (operator's choice)
    assert get_provider({"LOG_ON_LLM_PROVIDER": "groq", "LOG_ON_LLM_API_KEY": "k"}).config.api_key == "k"
    for p in (g, o):
        d = json.dumps(p.config.describe())
        assert FAKE_GROQ not in d and FAKE_OR not in d and FAKE_GROQ not in repr(p) and FAKE_OR not in repr(p)


def test_openrouter_sends_attribution_headers_and_extra_body():
    with FakeServer() as fs:
        p = make_provider("openrouter", base_url=fs.base_url, env={"OPENROUTER_API_KEY": FAKE_OR, **NO_PACING})
        r = p.chat([{"role": "user", "content": "hi"}])
        assert r.content
        h = {k.lower(): v for k, v in fs.last_headers.items()}
        assert h["authorization"] == f"Bearer {FAKE_OR}"
        assert h["http-referer"] == OPENROUTER_HEADERS["HTTP-Referer"] and h["x-title"] == "LOG_ON"
        assert h["x-openrouter-title"] == "LOG_ON"
        assert fs.last_body["provider"] == {"require_parameters": True}
        stream_once(p, "sys", "plan something", max_tokens=8)  # the streaming bench path sends them too
        assert {k.lower() for k in fs.last_headers} >= {"http-referer", "x-title"} and fs.last_body["stream"] is True


def test_groq_extra_body_and_no_openrouter_headers():
    with FakeServer() as fs:
        p = make_provider("groq", base_url=fs.base_url, env={"GROQ_API_KEY": FAKE_GROQ, **NO_PACING})
        p.chat([{"role": "user", "content": "hi"}])
        assert fs.last_body["reasoning_effort"] == "low" and fs.last_body["include_reasoning"] is False
        assert "http-referer" not in {k.lower() for k in fs.last_headers}
        q = make_provider("groq-qwen", base_url=fs.base_url, env={"GROQ_API_KEY": FAKE_GROQ, **NO_PACING})
        q.chat([{"role": "user", "content": "hi"}])
        assert fs.last_body["reasoning_effort"] == "none" and fs.last_body["model"] == "qwen/qwen3.8-27b"


def test_429_is_retried_honouring_retry_after():
    with FakeServer(rate_limit_first=2, rate_limit_headers={"Retry-After": "0.2"}) as fs:
        p = make_provider("groq", base_url=fs.base_url, env={"GROQ_API_KEY": FAKE_GROQ, **NO_PACING})
        t0 = time.monotonic()
        r = p.chat([{"role": "user", "content": "hi"}])
        assert r.content and fs.requests == 3
        assert time.monotonic() - t0 >= 0.39
        assert p.stats["rate_limited"] == 2 and p.stats["rate_limit_wait_s"] >= 0.39


def test_groq_reset_header_used_when_no_retry_after():
    with FakeServer(rate_limit_first=1, rate_limit_headers={"x-ratelimit-reset-tokens": "50ms"}) as fs:
        p = make_provider("groq", base_url=fs.base_url, env={"GROQ_API_KEY": FAKE_GROQ, **NO_PACING})
        assert p.chat([{"role": "user", "content": "hi"}]).content and p.stats["rate_limited"] == 1


def test_rate_limit_exhausted_raises():
    with FakeServer(rate_limit_first=99) as fs:
        p = make_provider("openrouter", base_url=fs.base_url, rate_limit_retries=2,
                          env={"OPENROUTER_API_KEY": FAKE_OR, **NO_PACING})
        with pytest.raises(RateLimitError):
            p.chat([{"role": "user", "content": "hi"}])
        assert fs.requests == 3 and p.stats["rate_limited"] == 3


def test_planner_falls_back_to_rules_when_rate_limited():
    from log_on.planner import LLMPlanner
    with FakeServer(rate_limit_first=99) as fs:
        p = make_provider("groq", base_url=fs.base_url, rate_limit_retries=0, env={"GROQ_API_KEY": FAKE_GROQ, **NO_PACING})
        pl = LLMPlanner(provider=p)
        assert pl.plan("What is the total revenue in Q3?") == pl.fallback.plan("What is the total revenue in Q3?")
        assert "RateLimitError" in pl.last_error and fs.requests == 1


def test_retry_after_parsing():
    assert retry_after_s({"Retry-After": "3"}, 0) == 3.0
    assert retry_after_s({"retry-after": "999"}, 0, cap=60) == 60
    assert 0 <= retry_after_s({"Retry-After": formatdate(time.time() + 5, usegmt=True)}, 0) <= 6
    assert retry_after_s({"x-ratelimit-reset-tokens": "7.66s"}, 0) == pytest.approx(7.91)
    assert retry_after_s({"x-ratelimit-reset-requests": "2m59.56s"}, 0) == 60  # capped
    assert retry_after_s({"x-ratelimit-reset-tokens": "120ms"}, 0) == pytest.approx(0.37)
    assert 1.5 <= retry_after_s({"X-RateLimit-Reset": str(int((time.time() + 2) * 1000))}, 0) <= 2.1
    for attempt in range(6):
        assert 0 <= retry_after_s({}, attempt, base=1.0, cap=10) <= min(10, 2 ** attempt)


def test_client_side_pacing():
    with FakeServer() as fs:
        p = make_provider("groq", base_url=fs.base_url, rpm=600, env={"GROQ_API_KEY": FAKE_GROQ})  # 0.1 s apart
        t0 = time.monotonic()
        for _ in range(4):
            p.chat([{"role": "user", "content": "hi"}])
        assert time.monotonic() - t0 >= 0.29 and p.stats["paced_wait_s"] > 0
    assert make_provider("groq", env={"GROQ_API_KEY": FAKE_GROQ}).config.rpm == 30   # free-tier default
    assert make_provider("groq", env={"GROQ_API_KEY": FAKE_GROQ, "LOG_ON_LLM_RPM": "0"}).config.rpm is None
    assert make_provider("groq", env={"GROQ_API_KEY": FAKE_GROQ, "LOG_ON_LLM_RPM": "120"}).config.rpm == 120


def test_per_provider_cost_rates(monkeypatch):
    monkeypatch.delenv("LOG_ON_PRICE_IN", raising=False)
    monkeypatch.delenv("LOG_ON_PRICE_OUT", raising=False)
    assert price_for("openai/gpt-oss-120b", "groq") == (0.15, 0.60)
    assert price_for("qwen/qwen3.8-27b", "groq-qwen") == (0.80, 4.00)
    assert price_for("qwen/qwen-2.5-72b-instruct", "openrouter") == (0.36, 0.40)
    assert price_for("google/gemma-4-31b-it:free", "openrouter-free") == (0.0, 0.0)
    assert price_for("anything:free", "openrouter") == (0.0, 0.0)
    assert price_for("qwen2.5-1.5b-instruct", "llamacpp") == (0.0, 0.0)
    assert llm_cost("openai/gpt-oss-120b", 1_000_000, 1_000_000, "groq") == pytest.approx(0.75)
    assert llm_cost("qwen/qwen-2.5-72b-instruct", 1000, 1000, "openrouter", reported=0.0123) == 0.0123
    u = add_usage(None, llm={"model": "qwen/qwen-2.5-72b-instruct", "provider": "openrouter", "prompt_tokens": 10,
                             "completion_tokens": 10, "cost_usd": 0.5})
    assert u["est_cost_usd"] == 0.5


def test_openrouter_reported_cost_flows_into_usage():
    with FakeServer(report_cost=0.00042) as fs:
        p = make_provider("openrouter", base_url=fs.base_url, env={"OPENROUTER_API_KEY": FAKE_OR, **NO_PACING})
        assert p.chat([{"role": "user", "content": "hi"}]).usage["cost_usd"] == 0.00042


def test_stratified_redteam_sample():
    attacks = generate()
    assert len(attacks) == 532
    s = stratified_sample(attacks, 100, seed=0)
    assert len(s) == 100 and len({a["id"] for a in s}) == 100
    cats = {a["category"] for a in attacks}
    assert {a["category"] for a in s} == cats                       # every category represented
    by = lambda xs, c: sum(a["category"] == c for a in xs)          # noqa: E731
    assert abs(by(s, "attack:prompt_injection") - 189 * 100 / 532) <= 2
    assert len({a["technique"] for a in s if a["category"] == "attack:jailbreak"}) >= 20  # spread over techniques
    assert [a["id"] for a in s] == [a["id"] for a in stratified_sample(attacks, 100, seed=0)]
    assert stratified_sample(attacks, 1000) == attacks


def test_parse_target():
    assert parse_target("groq") == {"preset": "groq", "model": None, "base_url": None, "label": "groq"}
    t = parse_target("openrouter=meta-llama/llama-3.3-70b-instruct@http://h:1/v1")
    assert t["model"] == "meta-llama/llama-3.3-70b-instruct" and t["base_url"] == "http://h:1/v1"


def test_compare_skips_targets_without_keys(tmp_path, capsys):
    out, md = tmp_path / "c.json", tmp_path / "c.md"
    rc = compare_main(["--targets", "groq,openrouter-free", "--out", str(out), "--md", str(md)], env={})
    assert rc == 3
    rep = json.loads(out.read_text())
    assert [t["skipped"] for t in rep["targets"]] == ["GROQ_API_KEY not set", "OPENROUTER_API_KEY not set"]
    assert "GROQ_API_KEY not set" in md.read_text()


def test_compare_end_to_end_on_fake_server(tmp_path, capsys):
    out, md = tmp_path / "c.json", tmp_path / "c.md"
    with FakeServer(rate_limit_first=1) as fs:
        env = {"GROQ_API_KEY": FAKE_GROQ, "OPENROUTER_API_KEY": FAKE_OR, **NO_PACING}
        rc = compare_main(["--targets", f"groq@{fs.base_url},openrouter-free@{fs.base_url}",
                           "--eval-only", "N01,N03,N04", "--redteam-sample", "6", "--requests", "2",
                           "--concurrency", "1", "--out", str(out), "--md", str(md)], env=env)
    assert rc == 0
    text = out.read_text() + md.read_text() + capsys.readouterr().out
    assert FAKE_GROQ not in text and FAKE_OR not in text                  # keys never written or printed
    rep = json.loads(out.read_text())
    for t in rep["targets"]:
        assert not t.get("skipped") and t["provider"]["api_key"] == "***"
        assert t["bench"]["levels"][0]["ok"] == 2
        assert t["eval"]["cases"] == 3 and t["eval"]["planner"]["planner_calls"] >= 1
        assert t["eval"]["planner"]["planner_validity"] == 1.0
        assert t["redteam"]["summary"]["attacks"] == 6 and t["redteam"]["summary"]["vulnerable"] == 0
    assert rep["targets"][0]["http"]["rate_limited"] == 1
    assert rep["targets"][1]["planner_cost_usd"] == 0.0                    # :free model
    assert "| target | model |" in md.read_text()


def test_evals_cli_llm_planner_on_fake_server(capsys):
    from log_on.evals.__main__ import main as evals_main
    with FakeServer() as fs:
        rc = evals_main(["--only", "N01,N03", "--planner", "llm", "--provider", "llamacpp", "--base-url", fs.base_url])
    out = capsys.readouterr().out
    assert rc == 0 and "planner: llm on llamacpp" in out and '"planner_calls": 2' in out
