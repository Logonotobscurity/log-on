"""Compare hosted / served LLM providers on the same LOG_ON workload.

    python -m log_on.serving.compare --targets groq,groq-qwen,openrouter,openrouter-free
    python -m log_on.serving.compare --targets groq=openai/gpt-oss-20b --skip eval --redteam-sample 50
    python -m log_on.serving.compare --targets llamacpp@http://127.0.0.1:8082/v1 --redteam-full

For every target (`preset[=model][@base_url]`) it runs, in order:
  1. bench     streaming latency / throughput on planner prompts at small concurrency (default 1,2; free tiers)
  2. eval      the full eval suite with an LLM planner -> eval gate verdict + planner validity
               (planner validity = model plans that parsed and passed the tool allow-list / planner LLM calls;
               every invalid or failed call silently falls back to the rule planner, so the gate verdict is only
               meaningful when the fallback rate is low; the report flags it otherwise)
  3. redteam   a seeded, category-stratified sample of the 532 generated attacks (default 100; --redteam-full)
and writes docs/provider_comparison.json + docs/provider_comparison.md.

Targets whose API key is not set are skipped with the reason (the key's env var name); key values are never
printed or written: reports contain `ProviderConfig.describe()`, which masks the key. Each provider instance paces
itself to the preset's free-tier RPM and retries 429s honouring Retry-After / reset headers (see providers.py).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path

from ..planner import LLMPlanner
from .providers import PRESETS, Provider, make_provider, preset_key

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TARGETS = "groq,groq-qwen,openrouter,openrouter-free"
HOSTED = {"groq", "groq-qwen", "openrouter", "openrouter-free", "openai", "xai"}
MAX_FALLBACK_FOR_VALID_GATE = 0.05

# Where each default model id was checked (2026-11-11); copied into the report.
MODEL_SOURCES = {
    "groq": "https://console.groq.com/docs/models (production model, $0.15/$0.60 per 1M; 30 RPM / 8K TPM free)",
    "groq-qwen": "https://console.groq.com/docs/models (preview model, $0.80/$4.00 per 1M; 30 RPM free) + "
                 "https://console.groq.com/docs/reasoning (reasoning_effort=none)",
    "openrouter": "https://openrouter.ai/api/v1/models + /api/v1/models/qwen/qwen-2.5-72b-instruct/endpoints "
                  "($0.36/$0.40 per 1M, 32K ctx, response_format supported)",
    "openrouter-free": "https://openrouter.ai/google/gemma-4-31b-it:free (free; 20 RPM, 50/day under 10 credits)",
}


class Meter:
    """Thread-safe counters shared by every planner instance of one target."""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = self.valid = self.fallbacks = 0
        self.prompt_tokens = self.completion_tokens = 0
        self.cost_usd = 0.0
        self.latency_ms: list[float] = []
        self.errors: dict[str, int] = defaultdict(int)

    def record(self, ok: bool, usage: dict | None, latency_ms: float, error: str | None, cost: float) -> None:
        with self._lock:
            self.calls += 1
            self.valid += ok
            self.fallbacks += not ok
            self.latency_ms.append(latency_ms)
            if usage:
                self.prompt_tokens += int(usage.get("prompt_tokens", 0))
                self.completion_tokens += int(usage.get("completion_tokens", 0))
            self.cost_usd += cost
            if error:
                self.errors[error.split(":")[0][:60]] += 1

    def summary(self) -> dict:
        with self._lock:
            lat = sorted(self.latency_ms)
            pct = lambda p: round(lat[min(len(lat) - 1, int(p * len(lat)))], 1) if lat else None  # noqa: E731
            return {"planner_calls": self.calls, "valid_plans": self.valid, "fallbacks": self.fallbacks,
                    "planner_validity": round(self.valid / self.calls, 4) if self.calls else None,
                    "fallback_rate": round(self.fallbacks / self.calls, 4) if self.calls else None,
                    "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                    "cost_usd": round(self.cost_usd, 6), "plan_latency_p50_ms": pct(0.5),
                    "plan_latency_p95_ms": pct(0.95), "error_types": dict(self.errors)}


class MeteredLLMPlanner(LLMPlanner):
    """LLMPlanner that reports every planning call (valid model plan vs rule fallback, tokens, cost) to a Meter."""

    def __init__(self, provider: Provider, meter: Meter, **kw):
        super().__init__(provider=provider, **kw)
        self.meter = meter

    def plan(self, goal, context=None):
        from ..telemetry.cost import llm_cost
        self.last_usage = None
        st = self.provider.stats
        waited0 = st["paced_wait_s"] + st["rate_limit_wait_s"]
        t0 = time.perf_counter()
        steps = super().plan(goal, context)
        waited = st["paced_wait_s"] + st["rate_limit_wait_s"] - waited0  # client pacing / 429 sleeps: not model time
        u = self.last_usage or {}
        cost = llm_cost(u.get("model"), u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                        u.get("provider"), u.get("cost_usd")) if u else 0.0
        self.meter.record(self.last_error is None, u, max(0.0, time.perf_counter() - t0 - waited) * 1000,
                          self.last_error, cost)
        return steps


def parse_target(spec: str) -> dict:
    """`preset[=model][@base_url]` -> {"preset", "model", "base_url", "label"}."""
    spec = spec.strip()
    base_url = None
    if "@" in spec:
        spec, base_url = spec.split("@", 1)
    preset, _, model = spec.partition("=")
    return {"preset": preset, "model": model or None, "base_url": base_url or None,
            "label": spec + (f"@{base_url}" if base_url else "")}


def key_status(preset: str, env: dict | None = None) -> tuple[bool, str]:
    """(has_key, reason). Never returns the key itself."""
    env = os.environ if env is None else env
    p = PRESETS.get(preset, {})
    if preset not in HOSTED:
        return True, "local / custom endpoint (no key needed)"
    if preset_key(preset, env):
        return True, f"{p.get('key_env', 'LOG_ON_LLM_API_KEY')} set"
    return False, f"{p.get('key_env', 'LOG_ON_LLM_API_KEY')} not set"


def stratified_sample(attacks: list[dict], n: int, seed: int = 0) -> list[dict]:
    """Proportional per-category sample (>= 1 per category), spread round-robin over techniques, seeded."""
    if n >= len(attacks):
        return list(attacks)
    rng = random.Random(seed)
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for a in attacks:
        by_cat[a["category"]].append(a)
    cats = sorted(by_cat)
    if n < len(cats):  # fewer slots than categories: one each from a seeded choice of categories
        cats = sorted(rng.sample(cats, n))
    alloc = {c: max(1, round(n * len(by_cat[c]) / len(attacks))) for c in cats}
    while sum(alloc.values()) > n:  # trim the largest allocations first, never below 1
        c = max((c for c in cats if alloc[c] > 1), key=lambda c: alloc[c])
        alloc[c] -= 1
    while sum(alloc.values()) < n:
        c = max((c for c in cats if alloc[c] < len(by_cat[c])), key=lambda c: len(by_cat[c]) - alloc[c])
        alloc[c] += 1
    out = []
    for c in cats:
        by_tech: dict[str, list[dict]] = defaultdict(list)
        for a in by_cat[c]:
            by_tech[a.get("technique") or a["id"]].append(a)
        techs = sorted(by_tech)
        rng.shuffle(techs)
        for t in techs:
            rng.shuffle(by_tech[t])
        picked, i = [], 0
        while len(picked) < min(alloc[c], len(by_cat[c])):
            t = techs[i % len(techs)]
            if by_tech[t]:
                picked.append(by_tech[t].pop())
            i += 1
        out += picked
    return out


def run_bench(prov: Provider, levels: list[int], n: int, max_tokens: int) -> dict:
    from .bench import benchmark
    # No client pacing during the bench: pacing would be counted as latency and cap c=2 throughput. The bench is
    # small (warmup + levels x n requests), so it stays inside a 30 RPM tier; 429s are still retried and counted.
    bp = prov.with_(rpm=None)
    rep = benchmark(bp, levels, n, max_tokens=max_tokens, warmup=1)
    for k, v in bp.stats.items():
        prov.stats[k] = round(prov.stats[k] + v, 2)
    return {"max_tokens": max_tokens, "levels": rep["levels"], "pacing": "off during bench",
            "rate_limited": bp.stats["rate_limited"]}


def sample_eval_cases(cases: list[dict], n: int, seed: int = 0) -> list[dict]:
    """n cases: all hand-written cases + a category-stratified sample of the frozen red-team regression cases; when n
    is smaller than the hand-written set, a category-stratified sample of everything (attack cases kept in)."""
    rt = [c for c in cases if str(c["id"]).startswith("RT")]
    core = [c for c in cases if not str(c["id"]).startswith("RT")]
    if n >= len(cases):
        return cases
    if n <= len(core):
        k = max(1, n // 4) if rt else 0  # a quarter from the 532 frozen attacks, the rest from hand-written cases
        picked = stratified_sample(core, n - k, seed) + (stratified_sample(rt, k, seed) if k else [])
        return picked[:n]
    return core + stratified_sample(rt, n - len(core), seed)


def run_eval(prov: Provider, work: Path, log, only: list[str] | None = None, sample: int | None = None,
             seed: int = 0, grading: str = "outcome") -> dict:
    from ..evals.__main__ import run_suite
    from ..evals.grader import check_thresholds, summarise
    from ..evals.runner import DEFAULT_CASES, build_graph, load_cases
    cases = load_cases()
    if only:
        keep = set(only)
        cases = [c for c in cases if c["id"] in keep]
    total = len(cases)
    if sample and not only:
        cases = sample_eval_cases(cases, sample, seed)
    meter = Meter()
    graph = build_graph(work)
    t0 = time.time()
    results = run_suite(cases, work, graph, lambda: MeteredLLMPlanner(prov, meter), log=lambda m: None,
                        grading=grading)
    summary = summarise(results)
    tpath = Path(DEFAULT_CASES).with_name("thresholds.json")
    thresholds = json.loads(tpath.read_text()) if tpath.exists() else {}
    fails = check_thresholds(summary, thresholds) if not only else []
    min_cases = thresholds.get("min_cases", 0)
    if len(cases) < total and len(cases) < min_cases:  # a quota-bound sample: judge quality, not suite size
        fails = [f for f in check_thresholds(summary, {**thresholds, "min_cases": 0})]
    pl = meter.summary()
    fb = pl["fallback_rate"]
    if only:
        verdict = "subset (thresholds not checked)"
    elif len(cases) < total:
        verdict = ("FAILED" if fails else "PASSED") + f" on a sample of {len(cases)}/{total} cases"
        if len(cases) < min_cases:
            verdict += f" (below min_cases={min_cases}: indicative only)"
    elif fails:
        verdict = "FAILED"
    else:
        verdict = "PASSED"
    if fb is not None and fb > MAX_FALLBACK_FOR_VALID_GATE:
        verdict += f" (inconclusive: {fb:.0%} of plans fell back to rules)"
    rm = summary.get("route_mode") or {}
    log(f"  eval: {len(results)} cases, gate {verdict} ({grading} grading; pass rate {summary['pass_rate']}, "
        f"route-mode {rm.get('pass_rate')}), planner validity {pl['planner_validity']}")
    keep = ("overall_score", "pass_rate", "attack_pass_rate", "pii_leaks", "metrics", "by_category",
            "total_tokens", "total_cost_usd", "mean_steps", "grading", "route_mode", "accepted_on_outcome")
    return {"cases": len(results), "cases_total": total, "gate": verdict, "threshold_failures": fails, "planner": pl,
            "summary": {k: summary.get(k) for k in keep},
            "failed_cases": [r["id"] for r in results if not r["passed"]],
            "accepted_on_outcome": [{"id": r["id"], "status": r["status"], "tools": r["tools"], "why": r["accepted"]}
                                    for r in results if r.get("accepted") not in (None, "route")],
            "failed_details": [{"id": r["id"], "category": r["category"], "status": r["status"], "tools": r["tools"],
                                "notes": {k: str(v)[:200] for k, v in r["notes"].items()
                                          if k in ("error", "outcome") or r["metrics"].get(k, 1) < 1}}
                               for r in results if not r["passed"]], "elapsed_s": round(time.time() - t0, 1)}


def run_redteam(prov: Provider, work: Path, sample: int | None, seed: int, log) -> dict:
    from ..redteam.attacks import generate
    from ..redteam.harness import run
    attacks = generate()
    picked = attacks if sample is None else stratified_sample(attacks, sample, seed)
    meter = Meter()
    rep = run(picked, lambda: MeteredLLMPlanner(prov, meter), workdir=work)
    s = rep["summary"]
    log(f"  redteam: {s['attacks']} attacks, {s['vulnerable']} vulnerable, {s['bypass_contained']} bypass")
    return {"sampled": sample is not None, "attacks_total": len(attacks), "seed": seed,
            "summary": {k: s[k] for k in ("attacks", "vulnerable", "bypass_contained", "defended",
                                          "attack_success_rate", "by_category")},
            "planner": meter.summary(), "elapsed_s": rep["elapsed_s"],
            "not_defended": [{k: r[k] for k in ("id", "category", "technique", "verdict", "findings", "status")}
                             for r in rep["results"] if r["verdict"] != "DEFENDED"]}


def run_target(t: dict, a, env: dict, log) -> dict:
    has_key, why = key_status(t["preset"], env)
    entry: dict = {"target": t["label"], "preset": t["preset"], "key": why}
    if t["preset"] not in PRESETS and not t["base_url"]:
        return {**entry, "skipped": f"unknown preset {t['preset']!r} and no @base_url"}
    if not has_key:
        return {**entry, "skipped": why}
    prov = make_provider(t["preset"], base_url=t["base_url"], model=t["model"], timeout=a.timeout, env=env)
    entry["provider"] = prov.config.describe()  # api_key masked
    entry["model_source"] = MODEL_SOURCES.get(t["preset"]) if not t["model"] else "user-specified"
    work = Path(tempfile.mkdtemp(prefix=f"log_on_compare_{t['preset']}_"))
    log(f"== {t['label']} ({prov.name}:{prov.model})")
    try:  # one real call first: a blocked / misconfigured target fails fast instead of burning the daily quota
        r = prov.complete("Reply with a JSON object.", 'Return {"ok": true}.')
        entry["preflight"] = {"ok": True, "latency_ms": r.latency_ms}
    except Exception as exc:  # noqa: BLE001
        entry["preflight"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        entry["http"] = dict(prov.stats)
        log(f"  preflight FAILED: {entry['preflight']['error']} - skipping this target")
        return entry
    steps = [s for s in ("bench", "eval", "redteam") if s not in a.skip]
    for step in steps:
        t0 = time.time()
        try:
            if step == "bench":
                entry["bench"] = run_bench(prov, a.concurrency, a.requests, a.max_tokens or prov.config.max_tokens)
                lv = entry["bench"]["levels"]
                log("  bench: " + ", ".join(f"c{r['concurrency']} p50 {r['latency_p50_ms']} ms ok {r['ok']}/"
                                             f"{r['requests']}" for r in lv))
                for r in lv:
                    if r.get("error_samples"):
                        log(f"  bench c{r['concurrency']} errors: {r['error_samples'][:2]}")
            elif step == "eval":
                entry["eval"] = run_eval(prov, work / "eval", log, a.eval_only.split(",") if a.eval_only else None,
                                         a.eval_sample, a.seed, a.grading)
            else:
                entry["redteam"] = run_redteam(prov, work / "redteam", None if a.redteam_full else a.redteam_sample,
                                               a.seed, log)
        except Exception as exc:  # noqa: BLE001 - one failing step must not lose the other results
            entry[step] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            log(f"  {step}: ERROR {entry[step]['error']}")
        entry.setdefault("step_seconds", {})[step] = round(time.time() - t0, 1)
    entry["http"] = {k: round(v, 1) if isinstance(v, float) else v for k, v in prov.stats.items()}  # requests / 429s / seconds waited on rate limits and pacing
    costs = [entry.get(s, {}).get("planner", {}).get("cost_usd", 0) for s in ("eval", "redteam")]
    entry["planner_cost_usd"] = round(sum(c or 0 for c in costs), 6)
    return entry


def _fmt(v, nd=None):
    if v is None or v == "":
        return "–"
    return f"{v:.{nd}f}" if nd is not None and isinstance(v, (int, float)) else str(v)


def _scope(t: dict, cfg: dict) -> str:
    cfg = t.get("run_config") or cfg
    e = t.get("eval") or {}
    r = (t.get("redteam") or {}).get("summary") or {}
    ev = f"eval {e['cases']}/{e.get('cases_total', e['cases'])}" if "cases" in e else "eval –"
    return (f"bench c={','.join(map(str, cfg.get('concurrency', [])))}×{cfg.get('requests')}, {ev}, "
            f"red team {r.get('attacks', '–')}" + (f", {cfg['rpm']} rpm" if cfg.get("rpm") else ""))


def _route_cell(e: dict) -> str:
    rm = (e.get("summary") or {}).get("route_mode")
    return f" (route {rm['pass_rate']})" if rm else ""


def merge(paths: list[str], notes: str | None = None) -> dict:
    """Combine several comparison JSON reports (e.g. runs with different quota-bound scopes) into one."""
    out = {"generated": time.strftime("%Y-%m-%d %H:%M %Z"), "config": {"merged_from": paths}, "targets": []}
    for p in paths:
        rep = json.loads(Path(p).read_text())
        for t in rep["targets"]:
            t.setdefault("run_config", rep.get("config", {}))
            t.setdefault("run_generated", rep.get("generated"))
            out["targets"].append(t)
    if notes:
        out["notes_md"] = Path(notes).read_text()
    return out


def to_markdown(rep: dict) -> str:
    cfg = rep.get("config", {})
    head = (f"bench concurrency {cfg['concurrency']} × {cfg['requests']} requests · red team: {cfg['redteam']}"
            if "concurrency" in cfg else "merged runs; scope per target below")
    L = ["# LOG_ON provider comparison", "",
         f"Generated {rep['generated']} · targets: {', '.join(t['target'] for t in rep['targets'])} · {head}", "",
         "| target | model | bench c=1 p50 / p95 ms | out tok/s (c=1) | bench valid plan | eval gate | eval pass rate "
         "| PII leaks | planner validity | red team vuln / bypass / n | planner cost $ | 429s (wait s) |",
         "|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|"]
    for t in rep["targets"]:
        if t.get("skipped"):
            L.append(f"| {t['target']} | – | skipped: {t['skipped']} | | | | | | | | | |")
            continue
        if not (t.get("preflight") or {}).get("ok", True):
            L.append(f"| {t['target']} | `{t['provider']['model']}` | preflight failed: {t['preflight']['error'][:80]} "
                     "| | | | | | | | | |")
            continue
        b = (t.get("bench") or {}).get("levels") or [{}]
        b1 = b[0] if b[0].get("ok") else {}
        e = t.get("eval") or {}
        r = (t.get("redteam") or {}).get("summary") or {}
        h = t.get("http", {})
        L.append(f"| {t['target']} | `{t['provider']['model']}` | {_fmt(b1.get('latency_p50_ms'))} / "
                 f"{_fmt(b1.get('latency_p95_ms'))} | {_fmt(b1.get('output_tok_per_s'))} | "
                 f"{_fmt(b1.get('valid_plan_rate'))} | {e.get('gate', e.get('error', '–'))} | "
                 f"{_fmt((e.get('summary') or {}).get('pass_rate'))}{_route_cell(e)} | {_fmt((e.get('summary') or {}).get('pii_leaks'))} | {_fmt((e.get('planner') or {}).get('planner_validity'))} | "
                 f"{_fmt(r.get('vulnerable'))} / {_fmt(r.get('bypass_contained'))} / {_fmt(r.get('attacks'))} | "
                 f"{_fmt(t.get('planner_cost_usd'), 4)} | {h.get('rate_limited', 0)} ({h.get('rate_limit_wait_s', 0)}) |")
    L += ["", "| target | scope |", "|---|---|"]
    for t in rep["targets"]:
        if not t.get("skipped"):
            L.append(f"| {t['target']} | {_scope(t, cfg)} |")
    L += ["", "Planner validity = model plans that parsed and used only allow-listed tools ÷ planner LLM calls; the "
          "rest fell back to the rule planner. An eval gate verdict marked *inconclusive* means more than "
          f"{MAX_FALLBACK_FOR_VALID_GATE:.0%} of plans came from the rule fallback, so it mostly measures the rules.",
          "", "## Models and sources", "", "| target | model | source (checked 2026-11-11) |", "|---|---|---|"]
    for t in rep["targets"]:
        m = (t.get("provider") or {}).get("model") or PRESETS.get(t["preset"], {}).get("model", "–")
        L.append(f"| {t['target']} | `{m}` | {t.get('model_source') or MODEL_SOURCES.get(t['preset'], '–')} |")
    for t in rep["targets"]:
        if t.get("skipped") or not (t.get("preflight") or {}).get("ok", True):
            continue
        L += ["", f"## {t['target']}", ""]
        for r in (t.get("bench") or {}).get("levels", []):
            L.append(f"- bench c={r['concurrency']}: {r['ok']}/{r['requests']} ok, p50 {r['latency_p50_ms']} ms, "
                     f"p95 {r['latency_p95_ms']} ms, TTFT p50 {r['ttft_p50_ms']} ms, {r['output_tok_per_s']} out tok/s"
                     + (f", errors {r.get('error_samples', [])[:2]}" if r.get("errors") else ""))
        for step in ("eval", "redteam"):
            s = t.get(step) or {}
            if "error" in s:
                L.append(f"- {step}: error {s['error']}")
            elif s:
                p = s.get("planner", {})
                extra = (f"gate {s['gate']}, {len(s.get('failed_cases', []))} failed cases"
                         if step == "eval" else
                         f"{s['summary']['vulnerable']} vulnerable, {s['summary']['bypass_contained']} bypass")
                L.append(f"- {step}: {extra}; planner calls {p.get('planner_calls')}, validity "
                         f"{p.get('planner_validity')}, tokens {p.get('prompt_tokens')}+{p.get('completion_tokens')}, "
                         f"${p.get('cost_usd')}, plan p50 {p.get('plan_latency_p50_ms')} ms, {s.get('elapsed_s')} s"
                         + (f", errors {p.get('error_types')}" if p.get("error_types") else ""))
        h = t.get("http", {})
        L.append(f"- HTTP: {h.get('requests', 0)} requests, {h.get('rate_limited', 0)} rate-limited, "
                 f"{h.get('rate_limit_wait_s', 0)} s waiting on 429s, {h.get('paced_wait_s', 0)} s client pacing")
    if rep.get("notes_md"):
        L += ["", rep["notes_md"].rstrip()]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None, env: dict | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m log_on.serving.compare", description=__doc__.split("\n")[0])
    ap.add_argument("--targets", default=DEFAULT_TARGETS, help="comma list of preset[=model][@base_url]")
    ap.add_argument("--skip", default="", help="comma list of steps to skip: bench,eval,redteam")
    ap.add_argument("--concurrency", default="1,2", help="bench concurrency levels (keep small on free tiers)")
    ap.add_argument("--requests", type=int, default=6, help="bench requests per level")
    ap.add_argument("--max-tokens", type=int, default=None, help="bench max_tokens (default: the preset's)")
    ap.add_argument("--redteam-sample", type=int, default=100, help="stratified attack sample size")
    ap.add_argument("--redteam-full", action="store_true", help="run all 532 attacks instead of a sample")
    ap.add_argument("--eval-only", help="comma list of eval case ids (subset: gate thresholds not checked)")
    ap.add_argument("--eval-sample", type=int, default=None,
                    help="all hand-written cases + a stratified sample of red-team regression cases, N in total "
                         "(to stay inside a daily request quota); thresholds are checked on the sample")
    ap.add_argument("--grading", choices=["outcome", "route"], default="outcome",
                    help="eval grading for LLM planners (outcome = safety invariants + acceptable outcomes)")
    ap.add_argument("--rpm", type=float, default=None, help="override client pacing (requests/min) for every target")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--out", default=str(ROOT / "docs" / "provider_comparison.json"))
    ap.add_argument("--md", default=str(ROOT / "docs" / "provider_comparison.md"))
    ap.add_argument("--dry-run", action="store_true", help="only show targets and whether their keys are set")
    ap.add_argument("--merge", help="comma list of comparison JSON files to combine into --out/--md (no API calls)")
    ap.add_argument("--notes", help="markdown file appended to the merged report")
    a = ap.parse_args(argv)
    if a.merge:
        rep = merge([x for x in a.merge.split(",") if x], a.notes)
        Path(a.out).write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n")
        Path(a.md).write_text(to_markdown(rep))
        print(to_markdown(rep))
        return 0
    env = dict(os.environ) if env is None else dict(env)
    if a.rpm is not None:
        env["LOG_ON_LLM_RPM"] = str(a.rpm)
    a.skip = {s.strip() for s in a.skip.split(",") if s.strip()}
    a.concurrency = [int(x) for x in a.concurrency.split(",") if x.strip()]
    targets = [parse_target(s) for s in a.targets.split(",") if s.strip()]
    log = lambda m: print(m, flush=True)  # noqa: E731
    if a.dry_run:
        for t in targets:
            p = PRESETS.get(t["preset"], {})
            log(f"{t['label']:<28} model {t['model'] or p.get('model')!s:<32} {key_status(t['preset'], env)[1]}")
        return 0
    os.environ.setdefault("LOG_ON_AS_OF", "2026-11-11")
    os.environ.setdefault("LOG_ON_TRACE_EXPORTERS", "jsonl")
    rep = {"generated": time.strftime("%Y-%m-%d %H:%M %Z"),
           "config": {"concurrency": a.concurrency, "requests": a.requests, "seed": a.seed,
                      "skip": sorted(a.skip), "eval_only": a.eval_only, "eval_sample": a.eval_sample,
                      "rpm": a.rpm,
                      "redteam": "full 532" if a.redteam_full else f"stratified sample of {a.redteam_sample}"},
           "targets": []}
    for t in targets:
        rep["targets"].append({**run_target(t, a, env, log), "run_config": rep["config"]})
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n")
    md = to_markdown(rep)
    if a.md:
        Path(a.md).write_text(md)
    print(md)
    ran = [t for t in rep["targets"] if not t.get("skipped")]
    return 0 if ran else 3  # 3 = nothing ran (e.g. no keys set)


if __name__ == "__main__":
    sys.exit(main())
