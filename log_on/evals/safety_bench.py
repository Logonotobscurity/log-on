"""Safety benchmark: regex-only vs combined (Presidio + ML classifier) detection, false positives and latency.

    python -m log_on.evals.safety_bench [--out docs/safety_bench.json]

Runs every corpus in evals/safety_bench.yaml (fake identifiers only) plus the red-team attack goals in two modes:
  regex     LOG_ON_PII_BACKEND=regex, LOG_ON_INJECTION_ML=off   (the Phase-4 layer, also the CI/offline fallback)
  combined  LOG_ON_PII_BACKEND=combined, LOG_ON_INJECTION_ML=auto (the default when the ML extras are installed)
Metrics: PII entity recall, held-out attacks never used to tune the regexes, + benign-text false positives (redact_text); prompt-injection detection on red-team goals and
false positives on benign requests (policy.check_input); quarantine false positives on benign tool output; per-call
latency (cold = first call on a fresh string, warm = cached)."""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import time
from pathlib import Path

import yaml

CORPUS = Path(__file__).resolve().parents[2] / "evals" / "safety_bench.yaml"
MODES = {"regex": {"LOG_ON_PII_BACKEND": "regex", "LOG_ON_INJECTION_ML": "off"},
         "combined": {"LOG_ON_PII_BACKEND": "combined", "LOG_ON_INJECTION_ML": "auto"}}
TAG_RE = re.compile(r"<([A-Z]+)>")


def _set_mode(name: str) -> None:
    os.environ.update(MODES[name])
    from ..guards import injection_ml, presidio_pii
    presidio_pii.redact.cache_clear()
    if injection_ml._clf is not None:
        injection_ml._clf.score.cache_clear()


def _timed(fn, items):
    out, ms = [], []
    for x in items:
        t0 = time.perf_counter()
        out.append(fn(x))
        ms.append((time.perf_counter() - t0) * 1000)
    return out, ms


def _lat(ms: list[float]) -> dict:
    if not ms:
        return {}
    s = sorted(ms)
    return {"mean_ms": round(statistics.mean(s), 2), "p50_ms": round(s[len(s) // 2], 2),
            "p95_ms": round(s[min(len(s) - 1, int(len(s) * 0.95))], 2)}


def run_mode(name: str, corpus: dict, attacks: list[str]) -> dict:
    _set_mode(name)
    from .. import policy
    from ..guards import injection_ml, presidio_pii
    from ..guards.validators import WITHHELD, InjectionQuarantine, redact_text
    # warm-up so model load time is not counted as per-request latency (reported separately)
    t0 = time.perf_counter()
    redact_text("warm up 08000000001")
    policy.check_input("warm up request")
    load_ms = round((time.perf_counter() - t0) * 1000, 1)

    # PII
    pos = corpus["pii_positive"]
    red, pii_ms = _timed(redact_text, [p["text"] for p in pos])
    found = expected = 0
    per_tag: dict[str, list[int]] = {}
    misses = []
    for p, r in zip(pos, red):
        tags = TAG_RE.findall(r)
        for e in p["expect"]:
            expected += 1
            ok = e in tags
            found += ok
            per_tag.setdefault(e, [0, 0])
            per_tag[e][0] += ok
            per_tag[e][1] += 1
            if not ok:
                misses.append({"text": p["text"], "expected": e, "got": r})
    neg_out, neg_ms = _timed(redact_text, corpus["pii_negative"])
    pii_fp = [{"text": t, "got": r} for t, r in zip(corpus["pii_negative"], neg_out) if r != t]

    # prompt injection on user requests
    ben_dec, ben_ms = _timed(policy.check_input, corpus["benign_requests"])
    req_fp = [{"text": t, "rule_ids": d["rule_ids"], "ml_score": d.get("ml_score")}
              for t, d in zip(corpus["benign_requests"], ben_dec) if d["rule_ids"]]
    atk_dec, atk_ms = _timed(policy.check_input, attacks)
    missed = [t for t, d in zip(attacks, atk_dec) if not d["rule_ids"]]
    ml_only = sum(1 for d in atk_dec if d["rule_ids"] == ["prompt_injection_ml"])

    # quarantine on untrusted tool output / retrieved docs
    q = InjectionQuarantine()
    q_out, q_ms = _timed(lambda t: q.validate(t), corpus["untrusted_benign"])
    q_fp = [{"text": t, "got": r.fix_value} for t, r in zip(corpus["untrusted_benign"], q_out)
            if not r.passed and WITHHELD in (r.fix_value or "")]
    from ..redteam.poison import ENCODED_INJECTION, INJECTION, PIDGIN_INJECTION
    poison = [f"Supplier note for Eko Pharma Distribution Ltd: {x}" for x in (INJECTION, PIDGIN_INJECTION,
                                                                                ENCODED_INJECTION)]
    p_caught = sum(1 for t in poison if not q.validate(t).passed)
    held = corpus.get("heldout_attacks", [])
    held_dec = [policy.check_input(t) for t in held]
    held_missed = [t for t, d in zip(held, held_dec) if not d["rule_ids"]]
    hu = corpus.get("heldout_untrusted", [])
    hu_missed = [t for t in hu if q.validate(t).passed]

    return {"mode": name, "env": MODES[name], "presidio": presidio_pii.status(), "classifier": injection_ml.status(),
            "warmup_ms": load_ms,
            "pii": {"recall": round(found / expected, 3), "found": found, "expected": expected,
                    "per_entity": {k: f"{v[0]}/{v[1]}" for k, v in sorted(per_tag.items())}, "misses": misses,
                    "false_positives": len(pii_fp), "negatives": len(corpus["pii_negative"]), "fp_examples": pii_fp,
                    "latency_positive": _lat(pii_ms), "latency_negative": _lat(neg_ms)},
            "injection": {"attacks": len(attacks), "detected": len(attacks) - len(missed),
                          "detection_rate": round(1 - len(missed) / len(attacks), 4), "ml_only_detections": ml_only,
                          "missed_examples": missed[:20],
                          "benign": len(corpus["benign_requests"]), "false_positives": len(req_fp),
                          "fp_rate": round(len(req_fp) / len(corpus["benign_requests"]), 4), "fp_examples": req_fp,
                          "latency_benign": _lat(ben_ms), "latency_attack": _lat(atk_ms)},
            "heldout": {"attacks": len(held), "detected": len(held) - len(held_missed), "missed": held_missed,
                        "untrusted": len(hu), "untrusted_quarantined": len(hu) - len(hu_missed),
                        "untrusted_missed": hu_missed},
            "quarantine": {"benign_tool_outputs": len(corpus["untrusted_benign"]), "false_positives": len(q_fp),
                           "fp_examples": q_fp, "poisoned_docs_caught": f"{p_caught}/{len(poison)}",
                           "latency": _lat(q_ms)}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m log_on.evals.safety_bench")
    ap.add_argument("--corpus", default=str(CORPUS))
    ap.add_argument("--out")
    ap.add_argument("--modes", default="regex,combined")
    a = ap.parse_args(argv)
    corpus = yaml.safe_load(Path(a.corpus).read_text())
    from ..redteam.attacks import generate
    attacks = [x["goal"] for x in generate() if x.get("expect_refusal")]
    saved = {k: os.environ.get(k) for m in MODES.values() for k in m}
    try:
        res = [run_mode(m, corpus, attacks) for m in a.modes.split(",")]
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    for r in res:
        p, i, q = r["pii"], r["injection"], r["quarantine"]
        print(f"[{r['mode']}] PII recall {p['found']}/{p['expected']} ({p['recall']:.0%}), PII FP {p['false_positives']}/"
              f"{p['negatives']}; injection detected {i['detected']}/{i['attacks']} ({i['detection_rate']:.1%}, "
              f"ML-only {i['ml_only_detections']}), request FP {i['false_positives']}/{i['benign']}; quarantine FP "
              f"{q['false_positives']}/{q['benign_tool_outputs']}, poisoned docs {q['poisoned_docs_caught']}; "
              f"held-out attacks {r['heldout']['detected']}/{r['heldout']['attacks']}, held-out poisoned text "
              f"{r['heldout']['untrusted_quarantined']}/{r['heldout']['untrusted']}; latency check_input p50 {i['latency_benign'].get('p50_ms')} ms, redact p50 "
              f"{p['latency_positive'].get('p50_ms')} ms")
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
