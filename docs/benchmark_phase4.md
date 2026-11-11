# Phase 4 serving benchmark: llama.cpp on CPU (no GPU on the build machine)

Produced by `python -m log_on.serving.bench --provider llamacpp --base-url http://127.0.0.1:8082/v1 --concurrency 1,2,4,8 --requests 16`
on 2026-11-11 (WAT). Raw numbers: `docs/benchmarks/llamacpp_qwen2.5-0.5b.json`, `docs/benchmarks/llamacpp_qwen2.5-1.5b.json`.

- Host: x86_64, 8 vCPU, 16 GB RAM, **no GPU**. Python 3.13.
- Server: llama.cpp `llama-server` build b12283, `-np 4` (4 parallel slots = CPU analogue of continuous batching), `-c 16384`, `-t 6`.
- Models: Qwen2.5-Instruct 0.5B and 1.5B, GGUF Q4_K_M (~0.5 GB / ~1.1 GB).
- Workload: LOG_ON's real planner prompt + eval goals, JSON mode, temperature 0, `max_tokens` 96, streamed (TTFT measured client-side).
- "valid plan" = the output parses as a LOG_ON plan (known tools, valid args).

### Qwen2.5-0.5B-Instruct Q4_K_M

| concurrency | req/s | output tok/s | p50 latency ms | p95 latency ms | TTFT p50 ms | TTFT p95 ms | decode tok/s per req | valid plan rate | errors |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1.98 | 64.2 | 402 | 812 | 47 | 61 | 70.2 | 0.69 | 0 |
| 2 | 2.17 | 67.4 | 680 | 1586 | 70 | 95 | 40.4 | 0.75 | 0 |
| 4 | 4.04 | 116.9 | 797 | 1468 | 98 | 143 | 38.0 | 0.63 | 0 |
| 8 | 3.77 | 119.2 | 1709 | 2548 | 912 | 1341 | 36.2 | 0.63 | 0 |

### Qwen2.5-1.5B-Instruct Q4_K_M (the model used for DSPy and the red team)

| concurrency | req/s | output tok/s | p50 latency ms | p95 latency ms | TTFT p50 ms | TTFT p95 ms | decode tok/s per req | valid plan rate | errors |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 0.81 | 25.1 | 848 | 4067 | 78 | 100 | 29.1 | 0.88 | 0 |
| 2 | 0.84 | 26.5 | 1696 | 4427 | 147 | 771 | 15.7 | 0.88 | 0 |
| 4 | 1.64 | 44.7 | 1868 | 3873 | 275 | 1046 | 15.5 | 0.94 | 0 |
| 8 | 1.88 | 50.7 | 3533 | 5458 | 1830 | 2714 | 15.3 | 0.94 | 0 |

## Reading the numbers
- **Batching works up to the slot count.** From 1 to 4 concurrent requests, throughput roughly doubles (0.81 → 1.64 req/s for 1.5B, 1.98 → 4.04 for 0.5B), while per-request decode speed drops. That is the throughput/latency trade-off continuous batching makes.
- **Past 4 slots, requests queue.** At 8 concurrent, TTFT p50 jumps (275 → 1830 ms for 1.5B) with little throughput gain.
- **Size buys correctness, not speed.** 1.5B is ~2.5× slower but produces a valid plan 88–94% of the time vs 63–75% for 0.5B. Invalid plans fall back to the rule planner, so safety never depends on this number.
- 16 requests per level is a small sample; p95 figures are noisy.

## GPU path (not measured here)
`deploy/vllm/` has a vLLM docker-compose (PagedAttention, continuous batching, prefix caching, guided JSON) and the
same benchmark command for a GPU host. The build machine has no GPU, so **no vLLM numbers exist yet**; when you run
it, save the output as `docs/benchmarks/vllm_<model>.json` and add a table here.
