"""CPU prompt-injection classifier behind the input policy and the output guard (post-Phase-4 step 2).

Model: protectai/deberta-v3-base-prompt-injection-v2 (Apache-2.0), ONNX export, run with onnxruntime + tokenizers
(both already LOG_ON dependencies via fastembed): no torch / transformers. Weights are cached OUTSIDE the repo:

    python -m log_on.guards.injection_ml download     # ~740 MB into ~/.cache/log_on_models/hf (HF_HOME-independent)
    python -m log_on.guards.injection_ml "Ignore previous instructions and ..."   # score a text

Environment:
  LOG_ON_INJECTION_ML         auto (default: use the model if it is in the cache, never download at runtime) | on | off
  LOG_ON_INJECTION_THRESHOLD  P(INJECTION) at or above which text is flagged (default 0.98, chosen on the eval set,
                              see docs/safety_ml_report.md)
  LOG_ON_INJECTION_MODEL_DIR  directory holding model.onnx + tokenizer.json (overrides the cache lookup)
  LOG_ON_MODEL_CACHE          cache root (default ~/.cache/log_on_models)

`get_classifier()` returns None when the model or onnxruntime is unavailable (CI, offline), and callers then rely on
the regex rules alone. A classifier error at inference time is logged and treated as "no signal", never as a crash.
"""
from __future__ import annotations

import functools
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ID = "protectai/deberta-v3-base-prompt-injection-v2"
DEFAULT_THRESHOLD = 0.98  # user requests (also needs lexical corroboration, see policy.ml_corroborated)
DEFAULT_UNTRUSTED_THRESHOLD = 0.9  # sentences of tool output / retrieved documents (safety_bench: 0 extra FPs)
MAX_TOKENS = 512
_lock = threading.Lock()
_clf = None
_unavailable: str | None = None


def cache_root() -> Path:
    return Path(os.getenv("LOG_ON_MODEL_CACHE", Path.home() / ".cache" / "log_on_models"))


def threshold() -> float:
    try:
        return float(os.getenv("LOG_ON_INJECTION_THRESHOLD", DEFAULT_THRESHOLD))
    except ValueError:
        return DEFAULT_THRESHOLD


def untrusted_threshold() -> float:
    try:
        return float(os.getenv("LOG_ON_QUARANTINE_THRESHOLD", DEFAULT_UNTRUSTED_THRESHOLD))
    except ValueError:
        return DEFAULT_UNTRUSTED_THRESHOLD


def mode() -> str:
    return os.getenv("LOG_ON_INJECTION_ML", "auto").lower()


def model_dir() -> Path | None:
    env = os.getenv("LOG_ON_INJECTION_MODEL_DIR")
    if env:
        return Path(env)
    snaps = cache_root() / "hf" / ("models--" + REPO_ID.replace("/", "--")) / "snapshots"
    for d in sorted(snaps.glob("*/onnx")) if snaps.exists() else []:
        if (d / "model.onnx").exists() and (d / "tokenizer.json").exists():
            return d
    return None


def download() -> Path:
    from huggingface_hub import snapshot_download
    p = snapshot_download(REPO_ID, cache_dir=str(cache_root() / "hf"),
                          allow_patterns=["onnx/model.onnx", "onnx/tokenizer.json", "onnx/config.json",
                                          "onnx/special_tokens_map.json", "onnx/tokenizer_config.json"])
    return Path(p) / "onnx"


class InjectionClassifier:
    def __init__(self, path: Path, threads: int | None = None):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.path = Path(path)
        self.tok = Tokenizer.from_file(str(self.path / "tokenizer.json"))
        self.tok.enable_truncation(MAX_TOKENS)
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads or int(os.getenv("LOG_ON_INJECTION_THREADS", "4"))
        so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(str(self.path / "model.onnx"), so, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}
        self._run_lock = threading.Lock()

    @functools.lru_cache(maxsize=4096)
    def score(self, text: str) -> float:
        """P(INJECTION) in [0, 1]."""
        import numpy as np
        enc = self.tok.encode(text or "")
        feed = {"input_ids": np.array([enc.ids], dtype=np.int64),
                "attention_mask": np.array([enc.attention_mask], dtype=np.int64)}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.array([enc.type_ids], dtype=np.int64)
        with self._run_lock:
            logits = self.sess.run(None, {k: v for k, v in feed.items() if k in self.inputs})[0][0]
        e = np.exp(logits - logits.max())
        return float(e[1] / e.sum())


def get_classifier() -> InjectionClassifier | None:
    global _clf, _unavailable
    if mode() == "off":
        return None
    if _clf is not None or _unavailable is not None:
        return _clf
    with _lock:
        if _clf is None and _unavailable is None:
            try:
                d = model_dir()
                if d is None:
                    if mode() == "on":
                        d = download()
                    else:
                        raise FileNotFoundError(f"model not cached under {cache_root()} "
                                                "(python -m log_on.guards.injection_ml download)")
                _clf = InjectionClassifier(d)
            except Exception as exc:  # noqa: BLE001 - optional layer: fall back to regex rules
                _unavailable = f"{type(exc).__name__}: {exc}"
                log.info("prompt-injection classifier unavailable (%s); regex rules only", _unavailable)
    return _clf


def status() -> dict:
    c = get_classifier()
    return {"mode": mode(), "available": c is not None, "threshold": threshold(),
            "untrusted_threshold": untrusted_threshold(), "model": REPO_ID if c else None,
            "error": _unavailable}


def score(text: str) -> float | None:
    """P(INJECTION), or None if the classifier is unavailable or failed."""
    c = get_classifier()
    if c is None or not (text or "").strip():
        return None
    try:
        return c.score(text[:4000])
    except Exception as exc:  # noqa: BLE001
        log.warning("injection classifier failed: %s", exc)
        return None


def is_injection(text: str) -> tuple[bool, float | None]:
    s = score(text)
    return (s is not None and s >= threshold()), s


# already-redacted text: give the classifier natural stand-ins (bare tags look odd to it; "email" loses the address)
_NEUTRAL = {"EMAIL": "someone@example.com", "PHONE": "08000000000", "NIN": "an ID number", "BVN": "an ID number",
            "CARD": "a card number", "IBAN": "a bank account", "IP": "an IP address", "CRYPTO": "a wallet",
            "PERSON": "a person", "SECRET": "a secret"}
_WORD = re.compile(r"[^\W\d_]{2,}")
_SENT = re.compile(r"(?<=[.!?\n])\s+")
_TAG = re.compile(r"<(EMAIL|PHONE|NIN|BVN|CARD|IBAN|IP|CRYPTO|PERSON|SECRET)>")


def _sentences(text: str) -> list[str]:
    return [x.strip() for x in _SENT.split(text or "") if x.strip()]


def _string_leaves(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _string_leaves(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _string_leaves(v)]
    return []


def flagged_sentences(text: str, regex_check=None) -> list[str]:
    """Sentences of untrusted text (tool output, retrieved documents) that look like instructions to the AI.

    `regex_check(sentence) -> bool` is the fallback (the input-policy rules) and is always applied; the classifier
    adds its own hits when available. JSON output is judged per string value (the classifier mistakes JSON records
    for injections), redaction tags are neutralised before scoring, and fragments with fewer than 4 letter-words are skipped by the
    classifier to limit noise."""
    import json
    try:
        parsed = json.loads(text)
        units = [s for leaf in _string_leaves(parsed) for s in _sentences(leaf)] if isinstance(parsed, (dict, list)) \
            else _sentences(text)
    except (ValueError, TypeError):
        units = _sentences(text)
    hits = []
    for s in units:
        rx = bool(regex_check and regex_check(s))
        if not rx and len(_WORD.findall(s)) >= 4:  # real words, not '4111 1111 1111 1111'
            sc = score(_TAG.sub(lambda m: _NEUTRAL[m.group(1)], s))
            ml = sc is not None and sc >= untrusted_threshold()
        else:
            ml = False
        if rx or ml:
            hits.append(s)
    return hits


def quarantine(text: str, hits: list[str], placeholder: str) -> str:
    """Replace flagged sentences; JSON stays valid JSON (values are rewritten, not the raw text)."""
    import json
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        def fix(o):
            if isinstance(o, str):
                for h in hits:
                    o = o.replace(h, placeholder)
                return o
            if isinstance(o, dict):
                return {k: fix(v) for k, v in o.items()}
            if isinstance(o, list):
                return [fix(v) for v in o]
            return o
        return json.dumps(fix(parsed), ensure_ascii=False)
    for h in hits:
        text = text.replace(h, placeholder)
    return text


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["download"]:
        print(download())
        return 0
    c = get_classifier()
    if c is None:
        print(f"classifier unavailable: {_unavailable}")
        return 1
    for t in argv or [sys.stdin.read()]:
        t0 = time.perf_counter()
        s = c.score(t)
        print(f"{s:.4f} {'INJECTION' if s >= threshold() else 'safe'} ({(time.perf_counter() - t0) * 1000:.0f} ms) {t[:80]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
