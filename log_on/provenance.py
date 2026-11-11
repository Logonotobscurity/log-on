"""Ticket O-3 (post-Phase-4 step 3): which code, policy, prompt and model made each decision.

Every audit record carries a `prov` block, inside the hash-chained body (so it is tamper-evident like the rest):

    {"code": "<git sha, or $LOG_ON_GIT_SHA in a container>", "policy_version": POLICY_VERSION,
     "policy_hash": "<sha256 of the policy/permission/guard sources + the safety settings in force>",
     "prompt": "<planner prompt id: 'rules', 'llm:sha256:<12>' or 'dspy:<registry version>'>",
     "planner": "...", "provider": "...", "model": "..."}

`python -m log_on.audit verify runs/audit.jsonl` checks the chain and that every record from the first stamped one
onward is stamped (older, pre-O-3 records are reported, not failed).
"""
from __future__ import annotations

import functools
import hashlib
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Bump on any intentional change to the input policy, permissions or guard behaviour (the hash catches the rest).
POLICY_VERSION = "2026.11-step3"
POLICY_SOURCES = ("policy.py", "permissions.py", "guards/validators.py", "guards/presidio_pii.py",
                  "guards/injection_ml.py")
POLICY_ENV = ("LOG_ON_TRUSTED_DOMAINS", "LOG_ON_PII_BACKEND", "LOG_ON_INJECTION_ML", "LOG_ON_INJECTION_THRESHOLD",
              "LOG_ON_QUARANTINE", "LOG_ON_QUARANTINE_THRESHOLD", "LOG_ON_PII_PERSON")


@functools.lru_cache(maxsize=1)
def code_sha() -> str:
    """Git commit of the running code: $LOG_ON_GIT_SHA (set at docker build time), else `git rev-parse HEAD`
    (with '-dirty' if tracked files are modified), else 'unknown'."""
    env = os.getenv("LOG_ON_GIT_SHA", "").strip()
    if env:
        return env
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5,
                             check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except Exception:  # noqa: BLE001 - no git / not a checkout (e.g. a container without LOG_ON_GIT_SHA)
        return "unknown"


def policy_hash() -> str:
    return _policy_hash(tuple(os.getenv(k, "") for k in POLICY_ENV))


@functools.lru_cache(maxsize=32)
def _policy_hash(env_values: tuple) -> str:
    h = hashlib.sha256(POLICY_VERSION.encode())
    for rel in POLICY_SOURCES:
        p = ROOT / rel
        h.update(rel.encode() + b"\0" + (p.read_bytes() if p.exists() else b"") + b"\0")
    for k, v in zip(POLICY_ENV, env_values):  # settings change behaviour too
        h.update(f"{k}={v}\0".encode())
    return h.hexdigest()[:16]


def prompt_id(planner) -> str:
    version = getattr(planner, "version", None)
    if version:
        return f"dspy:{version}"
    sp = getattr(planner, "system_prompt", None)
    if sp:
        return "llm:sha256:" + hashlib.sha256(sp.encode()).hexdigest()[:12]
    return getattr(planner, "name", None) or type(planner).__name__


def stamp(planner=None) -> dict:
    prov = getattr(planner, "provider", None)
    return {"code": code_sha(), "policy_version": POLICY_VERSION, "policy_hash": policy_hash(),
            "prompt": prompt_id(planner) if planner is not None else "none",
            "planner": getattr(planner, "name", None) if planner is not None else "none",
            "provider": getattr(planner, "provider_name", None) or getattr(prov, "name", None) or "none",
            "model": getattr(prov, "model", None) or getattr(planner, "model", None) or "none"}
