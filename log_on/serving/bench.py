"""Throughput / latency benchmark for any OpenAI-compatible provider (vLLM, llama.cpp, Ollama, fake).

    python -m log_on.serving.bench --provider llamacpp --concurrency 1,2,4,8 --requests 16 \
        --out docs/benchmarks/llamacpp_qwen2.5-1.5b.json

Workload = LOG_ON's real planner prompt (SYSTEM_PROMPT + eval goals), JSON mode, temperature 0, streamed so that
time-to-first-token (TTFT) is measured client-side. Per concurrency level it reports:
  req/s, aggregate output tok/s, latency p50/p95, TTFT p50/p95, per-request decode tok/s, errors, and
  the plan-validity rate (fraction of outputs that parse as a valid LOG_ON plan) - speed without validity is useless.
This is the course's (vLLM) measurement loop: fix the workload, sweep concurrency, watch throughput rise until
latency explodes. Continuous batching shows up as req/s growing with concurrency while p50 latency grows slowly.
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .providers import PRESETS, Provider, get_provider, make_provider

GOALS = ["Calculate 1250 * 0.075 + 40", "Look up customer C-1001", "Get the Q3 revenue report",
         "Send the weekly ops report to ops@acme.example", "Show the weekly ops report",
         "What is 17 * 23 - 4?", "Find customer C-1002 details", "Send the Q3 revenue report to finance@acme.example"]


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 1)


def stream_once(provider: Provider, system: str, user: str, max_tokens: int = 96) -> dict:
    c = provider.config
    body = {"model": c.model, "temperature": 0, "max_tokens": max_tokens, "stream": True,
            "stream_options": {"include_usage": True}, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    t0 = time.perf_counter()
    ttft, text, ctoks = None, [], None
    # Provider.open adds the preset's headers/extra body, client-side pacing and 429 retry-after handling
    with provider.open("/chat/completions", body, extra_headers={"Accept": "text/event-stream"}) as resp:
        ctype = resp.headers.get("Content-Type", "")
        if "event-stream" not in ctype:  # server ignored stream=True (e.g. the fake server)
            data = json.loads(resp.read())
            ttft = (time.perf_counter() - t0) * 1000
            text = [data["choices"][0]["message"]["content"]]
            ctoks = (data.get("usage") or {}).get("completion_tokens")
        else:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                ev = json.loads(payload)
                if ev.get("error"):  # e.g. OpenRouter mid-stream rate limit (HTTP 200 already sent)
                    raise RuntimeError(f"stream error {ev['error'].get('code')}: {str(ev['error'].get('message'))[:120]}")
                if ev.get("usage"):
                    ctoks = ev["usage"].get("completion_tokens")
                for ch in ev.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        if ttft is None:
                            ttft = (time.perf_counter() - t0) * 1000
                        text.append(piece)
    total = (time.perf_counter() - t0) * 1000
    out = "".join(text)
    ctoks = ctoks or max(1, len(out) // 4)
    decode_ms = max(total - (ttft or total), 1e-6)
    return {"latency_ms": total, "ttft_ms": ttft or total, "completion_tokens": ctoks,
            "decode_tps": ctoks / (decode_ms / 1000) if ctoks > 1 else 0.0, "output": out}


def valid_plan(text: str) -> bool:
    from ..planner import LLMPlanner
    from .providers import parse_json_lenient
    try:
        steps = LLMPlanner.parse(json.dumps(parse_json_lenient(text)))
        return bool(steps)
    except Exception:  # noqa: BLE001
        return False


def run_level(provider: Provider, concurrency: int, n: int, max_tokens: int) -> dict:
    from ..planner import SYSTEM_PROMPT
    jobs = [GOALS[i % len(GOALS)] for i in range(n)]
    results, errors = [], []

    def one(goal: str):
        try:
            return stream_once(provider, SYSTEM_PROMPT, goal, max_tokens)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
            return None

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for r in pool.map(one, jobs):
            if r:
                results.append(r)
    wall = time.perf_counter() - t0
    lat = [r["latency_ms"] for r in results]
    ttft = [r["ttft_ms"] for r in results]
    toks = sum(r["completion_tokens"] for r in results)
    return {"concurrency": concurrency, "requests": n, "ok": len(results), "errors": len(errors),
            "error_samples": errors[:3], "wall_s": round(wall, 2), "req_per_s": round(len(results) / wall, 2),
            "output_tok_per_s": round(toks / wall, 1), "latency_p50_ms": _pct(lat, 0.5),
            "latency_p95_ms": _pct(lat, 0.95), "ttft_p50_ms": _pct(ttft, 0.5), "ttft_p95_ms": _pct(ttft, 0.95),
            "decode_tok_per_s_per_req": round(statistics.mean([r["decode_tps"] for r in results]), 1) if results else 0,
            "valid_plan_rate": round(sum(valid_plan(r["output"]) for r in results) / len(results), 3) if results else 0}


def benchmark(provider: Provider, levels: list[int], n: int, max_tokens: int = 96, warmup: int = 1) -> dict:
    for _ in range(warmup):
        try:
            run_level(provider, 1, 1, max_tokens)
        except Exception:  # noqa: BLE001
            pass
    rows = [run_level(provider, c, max(n, c), max_tokens) for c in levels]
    return {"provider": provider.config.describe(), "max_tokens": max_tokens,
            "host": {"machine": platform.machine(), "python": platform.python_version(),
                     "cpus": __import__("os").cpu_count()},
            "date": time.strftime("%Y-%m-%d %H:%M %Z"), "levels": rows}


def to_markdown(rep: dict) -> str:
    p = rep["provider"]
    lines = [f"**{p['name']}** · model `{p['model']}` · {rep['host']['cpus']} CPUs · max_tokens {rep['max_tokens']}",
             "", "| conc | req | ok | req/s | out tok/s | p50 ms | p95 ms | TTFT p50 | TTFT p95 | tok/s/req | valid plan |",
             "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rep["levels"]:
        lines.append(f"| {r['concurrency']} | {r['requests']} | {r['ok']} | {r['req_per_s']} | {r['output_tok_per_s']} | "
                     f"{r['latency_p50_ms']} | {r['latency_p95_ms']} | {r['ttft_p50_ms']} | {r['ttft_p95_ms']} | "
                     f"{r['decode_tok_per_s_per_req']} | {r['valid_plan_rate']} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m log_on.serving.bench")
    ap.add_argument("--provider", help=f"preset: {sorted(PRESETS)} (default: from env)")
    ap.add_argument("--base-url")
    ap.add_argument("--model")
    ap.add_argument("--concurrency", default="1,2,4,8")
    ap.add_argument("--requests", type=int, default=16)
    ap.add_argument("--max-tokens", type=int, default=96)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    prov = make_provider(a.provider, base_url=a.base_url, model=a.model, timeout=300) if a.provider else get_provider()
    if prov is None:
        print("no provider configured (set --provider or LOG_ON_LLM_PROVIDER)")
        return 2
    rep = benchmark(prov, [int(x) for x in a.concurrency.split(",")], a.requests, a.max_tokens)
    print(to_markdown(rep))
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(rep, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
