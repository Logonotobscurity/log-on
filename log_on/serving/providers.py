"""Model serving layer: one OpenAI-compatible provider abstraction for every model call in LOG_ON.

The planner, the verifier, the reviser (via the planner), the KG extractor, DSPy and the red-team harness all
talk to a model through `Provider`. A provider is just an OpenAI-compatible `/v1/chat/completions` endpoint, so
the same code runs against:

  vllm      vLLM on a GPU host (PagedAttention + continuous batching)        http://localhost:8000/v1
  llamacpp  llama.cpp `llama-server` on CPU (GGUF, parallel slots)          http://127.0.0.1:8082/v1
  ollama    Ollama's OpenAI-compatible API                                   http://localhost:11434/v1
  openai    api.openai.com (needs a key)                                     https://api.openai.com/v1
  xai       api.x.ai (needs a key)                                           https://api.x.ai/v1
  groq      GroqCloud, key from GROQ_API_KEY                                 https://api.groq.com/openai/v1
  groq-qwen GroqCloud, Qwen model (preview)                                  https://api.groq.com/openai/v1
  openrouter       OpenRouter, key from OPENROUTER_API_KEY                   https://openrouter.ai/api/v1
  openrouter-free  OpenRouter ":free" model (20 req/min, 50-1000 req/day)    https://openrouter.ai/api/v1
  fake      log_on.serving.fake_server (deterministic, for tests/CI)         http://127.0.0.1:8099/v1

Selection (environment):
  LOG_ON_LLM_PROVIDER   one of the presets above, or "none" to force the rule-based path
  LOG_ON_LLM_BASE_URL   override the preset URL          LOG_ON_LLM_MODEL  override the preset model
  LOG_ON_LLM_API_KEY    key; otherwise the preset's own variable (GROQ_API_KEY, OPENROUTER_API_KEY, XAI_API_KEY,
                        OPENAI_API_KEY). A key is only ever sent to the provider it belongs to.
  LOG_ON_LLM_RPM        client-side request pacing (requests/minute); hosted free tiers have a preset default
Rate limits: HTTP 429 (and OpenRouter's transient 402 "in_flight_budget_exhausted", and 503) are retried after
`Retry-After` (or Groq's x-ratelimit-reset-* / OpenRouter's X-RateLimit-Reset), else exponential backoff with jitter.
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

OPENROUTER_HEADERS = {
    # App attribution (https://openrouter.ai/docs/app-attribution): HTTP-Referer identifies the app,
    # X-OpenRouter-Title names it (X-Title is the older, still-supported name). "hidden" keeps a new app out of the
    # public rankings while attribution still works. Override with LOG_ON_OPENROUTER_REFERER / _TITLE / _VISIBILITY.
    "HTTP-Referer": "https://github.com/Logonotobscurity/log-on",
    "X-OpenRouter-Title": "LOG_ON",
    "X-Title": "LOG_ON",
    "X-OpenRouter-App-Visibility": "hidden",
}

# Model IDs checked against each provider's docs on 2026-11-11 (see docs/provider_comparison.md for sources).
PRESETS: dict[str, dict] = {
    "vllm": {"base_url": "http://localhost:8000/v1", "model": "Qwen/Qwen2.5-1.5B-Instruct", "needs_key": False},
    "llamacpp": {"base_url": "http://127.0.0.1:8082/v1", "model": "qwen2.5-1.5b-instruct", "needs_key": False},
    "ollama": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:1.5b", "needs_key": False},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "needs_key": True,
               "key_env": "OPENAI_API_KEY"},
    "xai": {"base_url": "https://api.x.ai/v1", "model": "grok-4-fast", "needs_key": True, "key_env": "XAI_API_KEY"},
    # Groq: gpt-oss-120b is a production model on the self-serve plans (llama-3.3-70b-versatile is now listed as
    # Enterprise / contact sales). It is a reasoning model: low effort and no reasoning text keep JSON plans short;
    # reasoning tokens still count against max_tokens, hence 1024. Free tier: 30 req/min, 8K tokens/min.
    "groq": {"base_url": "https://api.groq.com/openai/v1", "model": "openai/gpt-oss-120b", "needs_key": True,
             "key_env": "GROQ_API_KEY", "max_tokens": 1024, "rpm": 30,
             "extra_body": {"reasoning_effort": "low", "include_reasoning": False}},
    # Groq's Qwen option (preview model): reasoning switched off for plain JSON plans.
    "groq-qwen": {"base_url": "https://api.groq.com/openai/v1", "model": "qwen/qwen3.8-27b", "needs_key": True,
                  "key_env": "GROQ_API_KEY", "rpm": 30, "extra_body": {"reasoning_effort": "none"}},
    # OpenRouter: require_parameters routes only to hosts that honour response_format (JSON mode).
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "model": "qwen/qwen-2.5-72b-instruct",
                   "needs_key": True, "key_env": "OPENROUTER_API_KEY", "headers": OPENROUTER_HEADERS,
                   "extra_body": {"provider": {"require_parameters": True}}},
    # No Qwen ":free" variant is listed on OpenRouter today; Gemma 4 31B (free) supports response_format.
    "openrouter-free": {"base_url": "https://openrouter.ai/api/v1", "model": "google/gemma-4-31b-it:free",
                        "needs_key": True, "key_env": "OPENROUTER_API_KEY", "headers": OPENROUTER_HEADERS, "rpm": 20,
                        "extra_body": {"provider": {"require_parameters": True}}},
    "fake": {"base_url": "http://127.0.0.1:8099/v1", "model": "log-on-fake", "needs_key": False},
}
RETRYABLE = {429, 503}


class ProviderError(RuntimeError):
    pass


class RateLimitError(ProviderError):
    """Still rate limited after every retry (HTTP 429 / transient 402 / 503)."""


def retry_after_s(headers, attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """Seconds to wait before retrying a rate-limited request: Retry-After (seconds or HTTP date), Groq's
    x-ratelimit-reset-requests/-tokens ("7.66s", "2m59.56s", "120ms"), OpenRouter's X-RateLimit-Reset (epoch ms),
    else exponential backoff with full jitter. Always capped."""
    import random
    from email.utils import parsedate_to_datetime
    h = {k.lower(): v for k, v in (dict(headers.items()) if headers is not None else {}).items()}
    ra = h.get("retry-after")
    if ra:
        try:
            return min(cap, max(0.0, float(ra)))
        except ValueError:
            try:
                return min(cap, max(0.0, parsedate_to_datetime(ra).timestamp() - time.time()))
            except (TypeError, ValueError):
                pass
    resets = [_duration_s(h[k]) for k in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests") if k in h]
    resets = [r for r in resets if r is not None]
    if resets:
        return min(cap, max(resets) + 0.25)
    if "x-ratelimit-reset" in h:
        try:
            return min(cap, max(0.0, float(h["x-ratelimit-reset"]) / 1000 - time.time()))
        except ValueError:
            pass
    return min(cap, random.uniform(0, base * (2 ** attempt)))


def _duration_s(v: str) -> float | None:
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?",
                     str(v).strip())
    if not m or not any(m.groups()):
        return None
    h, mi, se, ms = (float(x) if x else 0.0 for x in m.groups())
    return h * 3600 + mi * 60 + se + ms / 1000


class _Pacer:
    """Client-side request pacing per (base_url, model): at most `rpm` request starts per minute, shared by threads."""
    _lock = __import__("threading").Lock()
    _next: dict[tuple, float] = {}

    @classmethod
    def wait(cls, key: tuple, rpm: float | None) -> float:
        if not rpm:
            return 0.0
        with cls._lock:
            now = time.monotonic()
            start = max(now, cls._next.get(key, 0.0))
            cls._next[key] = start + 60.0 / rpm
        delay = start - now
        if delay > 0:
            time.sleep(delay)
        return delay


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
    headers: dict = field(default_factory=dict)      # extra request headers (e.g. OpenRouter attribution)
    extra_body: dict = field(default_factory=dict)   # provider-specific request fields (reasoning_effort, routing)
    rate_limit_retries: int = 5     # extra attempts after HTTP 429 / transient 402 / 503
    max_backoff_s: float = 60.0
    rpm: float | None = None        # client-side pacing (requests per minute), None = unpaced

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
        self.stats = {"requests": 0, "rate_limited": 0, "rate_limit_wait_s": 0.0, "paced_wait_s": 0.0}

    def request_headers(self, extra: dict | None = None) -> dict:
        c = self.config
        return {**c.headers, "Authorization": f"Bearer {c.api_key or 'local'}", "Content-Type": "application/json",
                **(extra or {})}

    def open(self, path: str, body: dict, extra_headers: dict | None = None, timeout: float | None = None):
        """POST with pacing and rate-limit retries; returns the open HTTP response (caller reads/closes it)."""
        c = self.config
        data = json.dumps({**c.extra_body, **body}).encode()
        for attempt in range(c.rate_limit_retries + 1):
            self.stats["paced_wait_s"] += _Pacer.wait((c.base_url, c.model), c.rpm)
            self.stats["requests"] += 1
            req = urllib.request.Request(f"{c.base_url.rstrip('/')}{path}", data=data, method="POST",
                                         headers=self.request_headers(extra_headers))
            try:
                return urllib.request.urlopen(req, timeout=timeout or c.timeout)
            except urllib.error.HTTPError as exc:
                transient_402 = exc.code == 402 and "in_flight" in _peek(exc)
                if (exc.code in RETRYABLE or transient_402) and attempt < c.rate_limit_retries:
                    wait = retry_after_s(exc.headers, attempt, cap=c.max_backoff_s)
                    self.stats["rate_limited"] += 1
                    self.stats["rate_limit_wait_s"] = round(self.stats["rate_limit_wait_s"] + wait, 2)
                    time.sleep(wait)
                    continue
                if exc.code in RETRYABLE or transient_402:
                    self.stats["rate_limited"] += 1
                    raise RateLimitError(f"{c.name}: HTTP {exc.code} after {attempt + 1} attempts") from exc
                raise
        raise RateLimitError(f"{c.name}: rate limited")  # pragma: no cover

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def model(self) -> str:
        return self.config.model

    def _post(self, path: str, body: dict) -> dict:
        with self.open(path, body) as resp:
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
            except RateLimitError:
                raise
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
        if isinstance(u.get("cost"), (int, float)):  # OpenRouter reports the billed cost in usage
            usage["cost_usd"] = float(u["cost"])
        return ChatResult(content=content, usage=usage, latency_ms=latency, raw=data)

    def complete(self, system: str, user: str, **kw) -> ChatResult:
        return self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], **kw)

    def chat_json(self, system: str, user: str, **kw) -> tuple[dict, dict]:
        r = self.complete(system, user, **kw)
        return parse_json_lenient(r.content), r.usage

    def health(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.config.base_url.rstrip('/')}/models", headers=self.request_headers())
            with urllib.request.urlopen(req, timeout=min(self.config.timeout, 5)) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001
            return False

    def with_(self, **kw) -> "Provider":
        return Provider(replace(self.config, **kw))

    def __repr__(self) -> str:
        return f"Provider({self.config.name}, {self.config.model} @ {self.config.base_url})"


def _peek(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read(2048).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    exc.fp = __import__("io").BytesIO(body.encode())  # keep it readable for callers
    return body


def preset_key(name: str, env: dict | None = None) -> str | None:
    """The key for a preset: LOG_ON_LLM_API_KEY, else the preset's own variable. Never another provider's key."""
    e = os.environ if env is None else env
    preset = PRESETS.get(name, {})
    return e.get("LOG_ON_LLM_API_KEY") or (e.get(preset["key_env"]) if preset.get("key_env") else None)


def _headers_for(name: str, env: dict) -> dict:
    h = dict(PRESETS.get(name, {}).get("headers") or {})
    if "HTTP-Referer" in h:
        for k, var in (("HTTP-Referer", "LOG_ON_OPENROUTER_REFERER"), ("X-OpenRouter-Title", "LOG_ON_OPENROUTER_TITLE"),
                       ("X-Title", "LOG_ON_OPENROUTER_TITLE"),
                       ("X-OpenRouter-App-Visibility", "LOG_ON_OPENROUTER_VISIBILITY")):
            if env.get(var) is not None:
                h[k] = env[var]
        h = {k: v for k, v in h.items() if v}
    return h


def make_provider(name: str = "openai", *, base_url: str | None = None, model: str | None = None,
                  api_key: str | None = None, env: dict | None = None, **kw) -> Provider:
    """`api_key` None -> the preset's own key variable (GROQ_API_KEY, OPENROUTER_API_KEY, ...), if set."""
    e = os.environ if env is None else env
    preset = PRESETS.get(name, {"base_url": base_url or "", "model": model or "", "needs_key": False})
    if "rpm" not in kw and e.get("LOG_ON_LLM_RPM"):
        kw["rpm"] = float(e["LOG_ON_LLM_RPM"]) or None  # 0 disables pacing
    for k in ("max_tokens", "rpm"):
        if k in preset and k not in kw:
            kw[k] = preset[k]
    kw.setdefault("headers", _headers_for(name, e))
    kw.setdefault("extra_body", dict(preset.get("extra_body") or {}))
    key = api_key or (preset_key(name, e) if name in PRESETS else None)
    return Provider(ProviderConfig(name=name, base_url=(base_url or preset["base_url"]).rstrip("/"),
                                   model=model or preset["model"], api_key=key or "local", **kw))


def get_provider(env: dict | None = None) -> Provider | None:
    """Provider from the environment, or None (= rule-based everywhere)."""
    e = os.environ if env is None else env
    name = (e.get("LOG_ON_LLM_PROVIDER") or "").strip().lower()
    key = e.get("LOG_ON_LLM_API_KEY") or e.get("OPENAI_API_KEY")  # back-compat: no provider named -> openai
    if name in ("none", "off", "rules"):
        return None
    if not name:
        if not key:
            return None
        name = "openai"
    preset = PRESETS.get(name)
    if preset is None and not e.get("LOG_ON_LLM_BASE_URL"):
        raise ValueError(f"unknown LOG_ON_LLM_PROVIDER={name!r}; choose one of {sorted(PRESETS)} or set LOG_ON_LLM_BASE_URL")
    if preset:
        key = preset_key(name, e) if preset.get("key_env") else e.get("LOG_ON_LLM_API_KEY")
    if preset and preset["needs_key"] and not key:
        return None
    kw = {}
    if e.get("LOG_ON_LLM_TIMEOUT"):
        kw["timeout"] = float(e["LOG_ON_LLM_TIMEOUT"])
    return make_provider(name, base_url=e.get("LOG_ON_LLM_BASE_URL"), model=e.get("LOG_ON_LLM_MODEL"),
                         api_key=key, env=e, **kw)
