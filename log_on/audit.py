"""Append-only, hash-chained JSONL audit log.

Every record carries `prev_hash` and `hash` (sha256 over the record body) so
after-the-fact edits or deletions are detectable with `verify()`.
Payloads are passed through the PII redactor before they are written, so the
audit trail itself never stores raw emails/phones/NINs/card numbers.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

GENESIS = "0" * 64


class AuditLog:
    def __init__(self, path: str | os.PathLike, redact: Callable[[str], str] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._redact = redact
        self._lock = threading.Lock()

    def _last_hash(self) -> str:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return GENESIS
        with self.path.open("rb") as f:
            last = f.read().splitlines()[-1]
        return json.loads(last)["hash"]

    def _scrub(self, obj: Any) -> Any:
        if self._redact is None:
            return obj
        if isinstance(obj, str):
            return self._redact(obj)
        if isinstance(obj, dict):
            return {k: self._scrub(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._scrub(v) for v in obj]
        return obj

    def record(self, run_id: str, node: str, event: str, data: dict[str, Any] | None = None) -> dict:
        with self._lock:
            body = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "run_id": run_id,
                "node": node,
                "event": event,
                "data": self._scrub(data or {}),
                "prev_hash": self._last_hash(),
            }
            body["hash"] = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
            # "a" mode: we only ever append; nothing in the codebase rewrites this file.
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(body, default=str) + "\n")
            return body

    def read(self, run_id: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        rows = [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()]
        return [r for r in rows if run_id is None or r["run_id"] == run_id]

    def verify(self) -> bool:
        prev = GENESIS
        for r in self.read():
            claimed = r.pop("hash")
            if r["prev_hash"] != prev:
                return False
            if hashlib.sha256(json.dumps(r, sort_keys=True, default=str).encode()).hexdigest() != claimed:
                return False
            prev = claimed
        return True
