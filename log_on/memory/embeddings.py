"""Local CPU embeddings for semantic memory search.

Default: fastembed + BAAI/bge-small-en-v1.5 (384-d, ONNX, ~67 MB, downloaded once to ~/.cache/fastembed,
never committed). Fallback: a deterministic hashing embedder (no download) for air-gapped CI
(LOG_ON_EMBEDDINGS=hash) or when fastembed is not installed.
"""
from __future__ import annotations

import functools
import hashlib
import math
import os
import re
from typing import Callable

DIMS = 384
MODEL = os.environ.get("LOG_ON_EMBED_MODEL", "BAAI/bge-small-en-v1.5")


def hash_embed(texts: list[str], dims: int = DIMS) -> list[list[float]]:
    out = []
    for t in texts:
        v = [0.0] * dims
        toks = re.findall(r"[a-z0-9]+", t.lower())
        for tok in toks + [a + "_" + b for a, b in zip(toks, toks[1:])]:
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % dims] += 1.0 if (h >> 8) & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


@functools.lru_cache(maxsize=2)
def _fastembed(model: str):
    from fastembed import TextEmbedding

    cache = os.environ.get("FASTEMBED_CACHE_PATH", os.path.expanduser("~/.cache/fastembed"))
    return TextEmbedding(model, cache_dir=cache)


def get_embedder(kind: str | None = None) -> tuple[Callable[[list[str]], list[list[float]]], str]:
    """Return (embed_fn, name). embed_fn(list[str]) -> list[list[float]] as LangGraph's index expects."""
    kind = (kind or os.environ.get("LOG_ON_EMBEDDINGS", "fastembed")).lower()
    if kind == "fastembed":
        try:
            m = _fastembed(MODEL)
            return (lambda texts: [list(map(float, v)) for v in m.embed(list(texts))]), f"fastembed:{MODEL}"
        except Exception:  # noqa: BLE001 - offline / not installed -> deterministic fallback
            pass
    return hash_embed, "hash-384"
