# Hosted providers: Groq and OpenRouter

LOG_ON talks to every model through one OpenAI-compatible `Provider` (`log_on/serving/providers.py`). On top of the
self-hosted presets (`vllm`, `llamacpp`, `ollama`, `fake`) and `openai` / `xai`, there are four hosted presets:

| preset | base URL | default model | key env var | client pacing | extra request body |
|---|---|---|---|---|---|
| `groq` | `https://api.groq.com/openai/v1` | `openai/gpt-oss-120b` | `GROQ_API_KEY` | 30 RPM | `reasoning_effort: low`, `include_reasoning: false` |
| `groq-qwen` | same | `qwen/qwen3.8-27b` | `GROQ_API_KEY` | 30 RPM | `reasoning_effort: none` |
| `openrouter` | `https://openrouter.ai/api/v1` | `qwen/qwen-2.5-72b-instruct` | `OPENROUTER_API_KEY` | – | `provider.require_parameters: true` |
| `openrouter-free` | same | `google/gemma-4-31b-it:free` | `OPENROUTER_API_KEY` | 20 RPM | `provider.require_parameters: true` |

Override the model with `LOG_ON_LLM_MODEL` (or `--model` / `preset=model` in the comparison), the pacing with
`LOG_ON_LLM_RPM` (`0` turns it off, e.g. on a paid tier).

## Model choice and sources (checked 2026-11-11)

- **Groq default `openai/gpt-oss-120b`**: a *production* model on https://console.groq.com/docs/models,
  $0.15 in / $0.60 out per 1M tokens, ~500 tok/s. The suggested `llama-3.3-70b-versatile` is now listed there
  as Enterprise / contact sales, so it is not usable on a self-serve key; pass `groq=llama-3.3-70b-versatile` if
  your account has it. Free-tier limits (https://console.groq.com/docs/rate-limits): 30 RPM, 1K requests/day,
  8K tokens/min, 200K tokens/day. Reasoning controls: https://console.groq.com/docs/reasoning.
- **Groq Qwen option `qwen/qwen3.8-27b`**: a *preview* model on the same page (may change without notice),
  $0.80 / $4.00 per 1M. Reasoning is switched off (`reasoning_effort: none`) so JSON plans are not preceded by
  thinking tokens.
- **OpenRouter default `qwen/qwen-2.5-72b-instruct`**: from https://openrouter.ai/api/v1/models, $0.36 / $0.40 per
  1M, 32K context, supports `response_format`. Its endpoints list
  (https://openrouter.ai/api/v1/models/qwen/qwen-2.5-72b-instruct/endpoints) showed a single upstream (DeepInfra,
  fp8) with ~73% uptime over the last 30 minutes when checked, so expect some failed calls.
- **OpenRouter free option `google/gemma-4-31b-it:free`**: OpenRouter has no Qwen `:free` model at the moment;
  this is the strongest free instruct model that supports `response_format`
  (https://openrouter.ai/google/gemma-4-31b-it:free, 262K context, $0). Free models are limited to 20 RPM and
  50 requests/day on accounts with under 10 credits purchased, 1000/day otherwise
  (https://openrouter.ai/docs/api-reference/limits) — a full comparison run needs the 1000/day tier.
- **OpenRouter headers**: `HTTP-Referer` (the public repo URL), `X-OpenRouter-Title` and `X-Title` (`LOG_ON`) and
  `X-OpenRouter-App-Visibility: hidden`, per https://openrouter.ai/docs/app-attribution. Override with
  `LOG_ON_OPENROUTER_REFERER`, `LOG_ON_OPENROUTER_TITLE`, `LOG_ON_OPENROUTER_VISIBILITY`.

## Keys

A hosted preset only ever sends its own key: `GROQ_API_KEY` goes to Groq, `OPENROUTER_API_KEY` to OpenRouter,
`OPENAI_API_KEY` / `XAI_API_KEY` to their own presets. `LOG_ON_LLM_API_KEY` is the explicit generic override.
Keys are masked (`***`) in `describe()`, `repr`, reports and logs. Keep them in the shell or a git-ignored `.env`.

## Rate limits and retries

`Provider.open()` (used by chat, JSON calls and the streaming benchmark) retries HTTP 429, 503 and OpenRouter's
transient 402 `in_flight_budget_exhausted` up to 5 times. The wait is, in order: `Retry-After` (seconds or HTTP
date), Groq's `x-ratelimit-reset-tokens` / `-requests` (`7.66s`, `2m59.56s`), OpenRouter's `X-RateLimit-Reset`
(epoch ms), else exponential backoff with full jitter; capped at 60 s. After the last retry it raises
`RateLimitError`, and the planner falls back to the rule planner for that call. Requests are also paced
client-side per (base URL, model) to the preset's RPM. `provider.stats` counts requests, 429s and seconds waited.
A mid-stream SSE `error` event (OpenRouter sends these after HTTP 200) fails the benchmark request.

## Cost

`log_on/telemetry/cost.py` prices calls per provider (`PROVIDER_PRICES`, the list prices above); any OpenRouter
`:free` model costs $0; when OpenRouter returns the billed `usage.cost` that number is used instead of the table.

## Comparison

```bash
export GROQ_API_KEY=...        # from the shell / .env, never committed
export OPENROUTER_API_KEY=...
.venv/bin/python -m log_on.serving.compare --dry-run                 # targets + whether each key is set
.venv/bin/python -m log_on.serving.compare                           # all four presets, writes docs/provider_comparison.{json,md}
.venv/bin/python -m log_on.serving.compare --targets groq,openrouter-free --redteam-full
.venv/bin/python -m log_on.evals --planner llm --provider groq       # eval gate alone with a hosted planner
```

Per target: (1) streaming benchmark on planner prompts at concurrency 1,2 × 6 requests; (2) the full eval suite
with the LLM as planner → eval gate verdict and **planner validity** (model plans that parsed and used only
allow-listed tools ÷ planner calls; the rest fell back to rules — a gate verdict with >5% fallbacks is marked
inconclusive); (3) a seeded red-team sample stratified by attack category and spread over techniques (100 of 532,
`--redteam-full` for all). Targets without a key are skipped with the env var name as the reason.
`scripts/compare_providers.py` is the same CLI. Expect a full four-target run to take well over an hour on free
tiers (the eval suite alone makes a few hundred planner calls per target); use `--skip`, `--targets` and
`--redteam-sample` to bound it, and run it in the background with a log.
