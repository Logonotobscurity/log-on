# Serving LOG_ON's model with vLLM (GPU) or llama.cpp (CPU)

Every LLM call in LOG_ON (planner, verifier, KG extractor, DSPy programs) goes through one
OpenAI-compatible `Provider` (`log_on/serving/providers.py`). Switching backends is an env change:

| Backend | Start | Env for LOG_ON |
|---|---|---|
| vLLM (GPU) | `docker compose -f deploy/vllm/docker-compose.yml up -d` or `vllm serve Qwen/Qwen2.5-7B-Instruct --max-model-len 8192 --enable-prefix-caching` | `LOG_ON_LLM_PROVIDER=vllm LOG_ON_LLM_MODEL=Qwen/Qwen2.5-7B-Instruct` (base URL defaults to `http://localhost:8000/v1`) |
| llama.cpp (CPU, used here) | `scripts/serve_llamacpp.sh 1.5b 8082` | `LOG_ON_LLM_PROVIDER=llamacpp LOG_ON_LLM_BASE_URL=http://127.0.0.1:8082/v1 LOG_ON_LLM_MODEL=qwen2.5-1.5b-instruct` |
| Ollama (CPU/GPU) | `ollama serve` + `ollama pull qwen2.5:1.5b` | `LOG_ON_LLM_PROVIDER=ollama LOG_ON_LLM_MODEL=qwen2.5:1.5b` |
| Hosted | — | `LOG_ON_LLM_PROVIDER=openai` (or `xai`) + `LOG_ON_LLM_API_KEY` |
| None | — | `LOG_ON_LLM_PROVIDER=none` → rule-based planner/verifier (the default with no key) |

If the server is down or returns something that isn't a valid plan, the planner falls back to the
rule-based planner, so the safety layers never depend on the model being up.

## Why vLLM is fast (what the flags do)
- **PagedAttention**: the KV cache is split into fixed-size blocks allocated on demand (like OS pages), so
  memory isn't reserved for `max_model_len` per request. Less fragmentation means more sequences fit in VRAM.
  `--gpu-memory-utilization` sets how much VRAM goes to weights + KV blocks.
- **Continuous batching**: new requests join the running batch at every decode step instead of waiting for
  the batch to finish. `--max-num-seqs` caps it. llama.cpp's `-np N` parallel slots are the CPU analogue
  (see `docs/benchmark_phase4.md`: throughput roughly doubles from 1 → 4 concurrent requests).
- **Prefix caching**: LOG_ON's planner prompt (system prompt + tool doc + DSPy demos) is identical for every
  request, so its KV blocks are computed once and reused. This cuts TTFT.
- **Guided decoding**: `response_format` with a JSON schema constrains tokens so plans always parse. On
  llama.cpp, `response_format: json_object` uses a GBNF grammar.

## Measure it
```bash
python -m log_on.serving.bench --provider vllm --model Qwen/Qwen2.5-7B-Instruct \
    --concurrency 1,4,16,32 --requests 64 --out docs/benchmarks/vllm_qwen2.5-7b.json
```
The bench streams requests to measure TTFT, sends concurrent planner prompts, and reports req/s, output tok/s,
p50/p95 latency and the share of replies that are valid LOG_ON plans.

Not run here: this environment has no GPU, so the vLLM numbers are not measured. The compose file
follows vLLM's documented OpenAI server flags. Check `vllm serve --help` for the image version you pin.
