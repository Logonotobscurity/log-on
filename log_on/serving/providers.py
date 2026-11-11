"""Model serving layer: one OpenAI-compatible provider abstraction for every model call in LOG_ON.

The planner, the verifier, the reviser (via the planner), the KG extractor, DSPy and the red-team harness all
talk to a model through `Provider`. A provider is just an OpenAI-compatible `/v1/chat/completions` endpoint, so
the same code runs against:

  vllm      vLLM on a GPU host (PagedAttention + continuous batching)        http://localhost:8000/v1
  llamacpp  llama.cpp `llama-server` on CPU (GGUF, parallel slots)          http://127.0.0.1:8082/v1
  ollama    Ollama's OpenAI-compatible API                                   http://localhost:11434/v1
  openai    api.openai.com (needs a key)                                     https://api.openai.com/v1
  xai       api.x.ai (needs a key)                                           https://api.x.ai/v1
  fake      log_on.serving.fake_server (deterministic, for tests/CI)         http://127.0.0.1:8099/v1

Selection (environment):
  LOG_ON_LLM_PROVIDER   one of the presets above, or "none" to force the rule-based path
  LOG_ON_LLM_BASE_URL   override the preset URL          LOG_ON_LLM_MODEL  override the preset model
  LOG_ON_LLM_API_KEY    key (or OPENAI_API_KEY); local presets need none
Back-compat: with no LOG_ON_LLM_PROVIDER but a key set, the "openai" preset is used (Phase 1-3 behaviour).
With nothing configured `get_provider()` returns None and every component uses its rule-based fallback.
Keys are never logged; `describe()` masks them.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field, replace

PRESETS: dict[str, dict] = {
    "vllm": {"base_url": "http://localhost:8000/v1", "model": "Qwen/Qwen2.5-1.5B-Instruct", "needs_key": False},
    "llamacpp": {"base_url": "http://127.0.0.1:8082/v1", "model": "qwen2.5-1.5b-instruct", "needs_key": False},
    "ollama": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:1.5b", "needs_key": False},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "needs_key": True},
    "xai": {"base_url": "https://api.x.ai/v1", "model": "grok-4-fast", "needs_key": True},
    "fake": {"base_url": "http://127.0.0.1:8099/v1", "model": "log-on-fake", "needs_key": False},
}


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    model: str
    api_key: str = field(default="local", repr=False)
    timeout: float = 60.0
    max_tokens: int | None = 512
    json_mode: bool = True          # send response_format=json_object (llama.cpp/vLLM/OpenAI all support it)
    retries: int = 1                # extra attempts on connection errors / 5xx

    def describe(self) -> dict:
        d = asdict(self)
        d["api_key"] = "***" if self.api_key and self.api_key != "local" else "none"
        return d


@dataclass
class ChatResult:
    content: str
    usage: dict
    latency_ms: float
    raw: dict = field(default_factory=dict, repr=False)


def parse_json_lenient(text: str) -> dict:
    """Small models sometimes wrap JSON in ``` fences or add prose; take the first balanced JSON object."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        v = json.loads(t)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                esc = (ch == "\\") and not esc
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(t[start:i + 1])
                        if isinstance(v, dict):
                            return v
                    except json.JSONDecodeError:
                        break
        start = t.find("{", start + 1)
    raise ValueError(f"no JSON object in model output: {text[:120]!r}")


class Provider:
    """Thin, dependency-free OpenAI-compatible chat client (urllib), shared by every model-backed component."""

    def __init__(self, config: ProviderConfig):
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def model(self) -> str:
        return self.config.model

    def _post(self, path: str, body: dict) -> dict:
        c = self.config
        req = urllib.request.Request(f"{c.base_url.rstrip('/')}{path}", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {c.api_key or 'local'}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=c.timeout) as resp:
            return json.loads(resp.read())

    def chat(self, messages: list[dict], *, json_mode: bool | None = None, temperature: float = 0.0,
             max_tokens: int | None = None) -> ChatResult:
        from ..telemetry.cost import est_tokens
        c = self.config
        want_json = c.json_mode if json_mode is None else json_mode
        body: dict = {"model": c.model, "temperature": temperature, "messages": messages}
        if max_tokens or c.max_tokens:
            body["max_tokens"] = max_tokens or c.max_tokens
        if want_json:
            body["response_format"] = {"type": "json_object"}
        last: Exception | None = None
        t0 = time.perf_counter()
        attempt, data = 0, None
        while data is None:
            try:
                data = self._post("/chat/completions", body)
                break
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code == 400 and "response_format" in body:  # endpoint without JSON mode: retry plain
                    body.pop("response_format")
                    continue
                if exc.code < 500:
                    raise ProviderError(f"{c.name}: HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last = exc
            attempt += 1
            if attempt > c.retries:
                raise ProviderError(f"{c.name} unavailable at {c.base_url}: {type(last).__name__}: {last}") from last
            time.sleep(0.2 * attempt)
        latency = round((time.perf_counter() - t0) * 1000, 1)
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{c.name}: malformed response") from exc
        u = data.get("usage") or {}
        prompt_text = "".join(str(m.get("content", "")) for m in messages)
        usage = {"model": c.model, "provider": c.name, "calls": 1,
                 "prompt_tokens": int(u.get("prompt_tokens") or est_tokens(prompt_text)),
                 "completion_tokens": int(u.get("completion_tokens") or est_tokens(content)),
                 "estimated": not bool(u), "latency_ms": latency}
        return ChatResult(content=content, usage=usage, latency_ms=latency, raw=data)

    def complete(self, system: str, user: str, **kw) -> ChatResult:
        return self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], **kw)

    def chat_json(self, system: str, user: str, **kw) -> tuple[dict, dict]:
        r = self.complete(system, user, **kw)
        return parse_json_lenient(r.content), r.usage

    def health(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.config.base_url.rstrip('/')}/models",
                                         headers={"Authorization": f"Bearer {self.config.api_key or 'local'}"})
            with urllib.request.urlopen(req, timeout=min(self.config.timeout, 5)) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001
            return False

    def with_(self, **kw) -> "Provider":
        return Provider(replace(self.config, **kw))

    def __repr__(self) -> str:
        return f"Provider({self.config.name}, {self.config.model} @ {self.config.base_url})"


def make_provider(name: str = "openai", *, base_url: str | None = None, model: str | None = None,
                  api_key: str | None = None, **kw) -> Provider:
    preset = PRESETS.get(name, {"base_url": base_url or "", "model": model or "", "needs_key": False})
    return Provider(ProviderConfig(name=name, base_url=(base_url or preset["base_url"]).rstrip("/"),
                                   model=model or preset["model"], api_key=api_key or "local", **kw))


def get_provider(env: dict | None = None) -> Provider | None:
    """Provider from the environment, or None (= rule-based everywhere)."""
    e = os.environ if env is None else env
    name = (e.get("LOG_ON_LLM_PROVIDER") or "").strip().lower()
    key = e.get("LOG_ON_LLM_API_KEY") or e.get("OPENAI_API_KEY")
    if name in ("none", "off", "rules"):
        return None
    if not name:
        if not key:
            return None
        name = "openai"
    preset = PRESETS.get(name)
    if preset is None and not e.get("LOG_ON_LLM_BASE_URL"):
        raise ValueError(f"unknown LOG_ON_LLM_PROVIDER={name!r}; choose one of {sorted(PRESETS)} or set LOG_ON_LLM_BASE_URL")
    if preset and preset["needs_key"] and not key:
        return None
    kw = {}
    if e.get("LOG_ON_LLM_TIMEOUT"):
        kw["timeout"] = float(e["LOG_ON_LLM_TIMEOUT"])
    return make_provider(name, base_url=e.get("LOG_ON_LLM_BASE_URL"), model=e.get("LOG_ON_LLM_MODEL"),
                         api_key=key, **kw)
