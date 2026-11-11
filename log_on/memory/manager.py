"""MemoryManager: the three memory types from the Long-Term Agentic Memory course, per user namespace.

  semantic   ("memories", user, "semantic")   facts & preferences ("my focus customer is Eko Pharma")
  episodic   ("memories", user, "episodic")   one redacted summary per completed run (what was asked, outcome)
  procedural ("memories", user, "procedural") instructions that change behaviour ("from now on keep answers
                                              brief") and lessons learned (an approver denied a recipient)

Backed by any LangGraph BaseStore: SqliteStore (durable across processes, sqlite-vec vector index) or
InMemoryStore. Semantic + episodic items are embedded for similarity search; procedural items are fetched
by key/listing.

Write policy (Phase 1 guardrails reused): every string is passed through the PII redactor before it is
stored or embedded, and anything that looks like a credential is rejected outright. So raw PII never
reaches the store, its vector index or the audit log.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from ..guards import redact_text
from ..guards.validators import SECRET_RE
from .embeddings import DIMS, get_embedder

# Stricter than the output guard: natural-language credential statements are never memorised.
MEMORY_SECRET_RE = re.compile(r"(?i)\b(password|passcode|passphrase|pin|otp|api[ _-]?key|secret|token|cvv)\b"
                              r"(?:\s+(?:for|of|on|to)\b[^.\n]{0,40}?)?\s*(\bis\b|\bwas\b|=|:)")
PII_TAG_RE = re.compile(r"<(EMAIL|PHONE|NIN|BVN|CARD|IBAN|IP|CRYPTO|PERSON|SECRET)>")


class MemoryWriteRejected(ValueError):
    pass


def make_memory_store(path: str | Path | None = None, embedder: str | None = None) -> tuple[BaseStore, str]:
    """SqliteStore at `path` (durable) or InMemoryStore if path is None. Returns (store, embedder_name)."""
    embed, name = get_embedder(embedder)
    index = {"dims": DIMS, "embed": embed, "fields": ["text"]}
    if path is None:
        return InMemoryStore(index=index), name
    from langgraph.store.sqlite import SqliteStore

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False, isolation_level=None)
    store = SqliteStore(conn, index=index)
    store.setup()
    return store, name


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_memory_intent(goal: str) -> dict[str, Any] | None:
    """Rule-based memory intents: remember <fact> | from now on <instruction> | recall."""
    g = goal.strip()
    m = re.match(r"(?i)^(?:please\s+)?(?:remember|note|keep in mind)(?:\s+that)?[:,]?\s+(.+)$", g)
    if m:
        return {"kind": "fact", "text": m.group(1).rstrip(".")}
    m = re.match(r"(?i)^(?:from now on|going forward|in future)[,:]?\s+(.+)$", g)
    if m:
        return {"kind": "instruction", "text": m.group(1).rstrip(".")}
    if re.search(r"(?i)\b(what do you (remember|know) about me|what did (i|we) (ask|discuss|do)|"
                 r"recall (my|our)|my (history|past requests))\b", g):
        return {"kind": "recall"}
    return None


def instruction_key(text: str) -> tuple[str, str]:
    """Map a free-text instruction to a procedural key/value the runtime understands."""
    t = text.lower()
    if re.search(r"\b(brief|short|concise|summar)", t):
        return "answer_style", "brief"
    if re.search(r"\b(detailed|full|verbose|everything)\b", t):
        return "answer_style", "detailed"
    return "note-" + hashlib.sha256(t.encode()).hexdigest()[:10], text


def recipient_key(recipient: str) -> str:
    """Procedural lessons about a recipient are keyed by a hash, so the raw address is never stored."""
    return "denied-recipient-" + hashlib.sha256(recipient.strip().lower().encode()).hexdigest()[:16]


class MemoryManager:
    def __init__(self, store: BaseStore, embedder_name: str = "unknown"):
        self.store = store
        self.embedder_name = embedder_name

    @staticmethod
    def ns(user: str, kind: str) -> tuple[str, ...]:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", user or "anonymous")
        return ("memories", safe, kind)

    # --- write guard ------------------------------------------------------------------------------
    @staticmethod
    def guard(text: str) -> tuple[str, list[str]]:
        if MEMORY_SECRET_RE.search(text) or SECRET_RE.search(text):
            raise MemoryWriteRejected("looks like a credential/secret; secrets are never stored in memory")
        from ..guards.injection_ml import is_injection
        from ..policy import looks_like_injection, ml_corroborated
        # post-Phase-4 step 2: no planted instructions (ML hit needs corroboration, as for user input)
        if looks_like_injection(text) or (is_injection(text)[0] and ml_corroborated([text])):
            raise MemoryWriteRejected("looks like an instruction to override the assistant's rules; not stored")
        clean = redact_text(text)
        tags = sorted(set(PII_TAG_RE.findall(clean)))
        if not re.sub(r"<[A-Z]+>|[\W_]", "", clean):
            raise MemoryWriteRejected("nothing left to remember after removing personal data")
        return clean, tags

    def _scrub(self, value: dict[str, Any]) -> dict[str, Any]:
        return {k: redact_text(v) if isinstance(v, str) else v for k, v in value.items()}

    # --- semantic -----------------------------------------------------------------------------------
    def remember_fact(self, user: str, text: str, run_id: str = "") -> dict[str, Any]:
        clean, redacted = self.guard(text)
        key = "fact-" + hashlib.sha256(clean.lower().encode()).hexdigest()[:12]  # same fact -> same key (dedupe)
        value = {"text": clean, "kind": "fact", "run_id": run_id, "created_at": _now(), "redacted": redacted}
        self.store.put(self.ns(user, "semantic"), key, value)
        return {"namespace": "semantic", "key": key, **value}

    # --- episodic -----------------------------------------------------------------------------------
    def add_episode(self, user: str, run_id: str, goal: str, status: str, tools: list[str], outcome: str,
                    intent: str | None = None) -> dict[str, Any]:
        value = self._scrub({"text": f"Asked: {goal} | outcome: {status}", "goal": goal, "status": status,
                             "tools": tools, "outcome": outcome[:300], "intent": intent or "task",
                             "run_id": run_id, "created_at": _now()})
        self.store.put(self.ns(user, "episodic"), f"ep-{run_id or uuid.uuid4().hex[:12]}", value)
        return {"namespace": "episodic", "key": f"ep-{run_id}", **value}

    # --- procedural ---------------------------------------------------------------------------------
    def set_instruction(self, user: str, text: str, run_id: str = "") -> dict[str, Any]:
        clean, _ = self.guard(text)
        key, val = instruction_key(clean)
        value = {"text": clean, "value": val, "run_id": run_id, "created_at": _now()}
        self.store.put(self.ns(user, "procedural"), key, value, index=False)
        return {"namespace": "procedural", "key": key, **value}

    def record_denial(self, user: str, recipient: str, tool: str, note: str, run_id: str) -> dict[str, Any]:
        key = recipient_key(recipient)
        value = self._scrub({"text": f"An approver denied {tool} to this recipient ({_now()[:10]}). Note: {note}"
                             .strip(), "value": "warn", "tool": tool, "run_id": run_id, "created_at": _now()})
        self.store.put(self.ns(user, "procedural"), key, value, index=False)
        return {"namespace": "procedural", "key": key, **value}

    def instructions(self, user: str) -> dict[str, dict[str, Any]]:
        return {it.key: it.value for it in self.store.search(self.ns(user, "procedural"), limit=100)}

    def denial_for(self, user: str, recipient: str) -> dict[str, Any] | None:
        it = self.store.get(self.ns(user, "procedural"), recipient_key(recipient))
        return it.value if it else None

    # --- retrieval ----------------------------------------------------------------------------------
    def search(self, user: str, query: str, kind: str = "semantic", limit: int = 3,
               min_score: float | None = None) -> list[dict[str, Any]]:
        items = self.store.search(self.ns(user, kind), query=query, limit=limit)
        out = [{"key": it.key, "score": round(it.score, 3) if it.score is not None else None, **it.value}
               for it in items]
        if min_score is not None:
            out = [o for o in out if o["score"] is None or o["score"] >= min_score]
        return out

    def recent_episodes(self, user: str, limit: int = 5) -> list[dict[str, Any]]:
        items = self.store.search(self.ns(user, "episodic"), limit=100)
        eps = sorted((it.value for it in items), key=lambda v: v.get("created_at", ""), reverse=True)
        return [e for e in eps if e.get("intent") not in ("recall",)][:limit]

    def all_facts(self, user: str) -> list[dict[str, Any]]:
        return [it.value for it in self.store.search(self.ns(user, "semantic"), limit=100)]
