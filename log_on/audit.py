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
    def __init__(self, path: str | os.PathLike, redact: Callable[[str], str] | None = None,
                 provenance: dict | Callable[[], dict] | None = None):
        """`provenance` (ticket O-3): a dict, or a callable returning one, stamped into every record as `prov`
        (inside the hashed body). See log_on.provenance."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._redact = redact
        self._prov = provenance
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
            prov = self._prov() if callable(self._prov) else self._prov
            if prov:
                body["prov"] = prov
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
        return self.verify_report()["ok"]

    def verify_report(self, require_provenance: bool = False) -> dict:
        """Chain check plus O-3 provenance check. Records before the first stamped one (older logs) are counted as
        `legacy`; once stamping starts, an unstamped record is an error (stamps can't be dropped silently)."""
        prev, n, legacy, errors, stamped_seen = GENESIS, 0, 0, [], False
        versions: dict[str, int] = {}
        for i, r in enumerate(self.read(), 1):
            n += 1
            claimed = r.pop("hash", None)
            if r.get("prev_hash") != prev:
                errors.append(f"record {i}: prev_hash does not match the previous record (deleted/reordered?)")
            if hashlib.sha256(json.dumps(r, sort_keys=True, default=str).encode()).hexdigest() != claimed:
                errors.append(f"record {i}: hash mismatch (edited after writing)")
            prov = r.get("prov")
            if prov:
                stamped_seen = True
                missing = [k for k in PROV_KEYS if not prov.get(k)]
                if missing:
                    errors.append(f"record {i}: provenance missing {missing}")
                key = f"{prov.get('code')}|{prov.get('policy_version')}|{prov.get('policy_hash')}|{prov.get('prompt')}"
                versions[key] = versions.get(key, 0) + 1
            elif stamped_seen or require_provenance:
                errors.append(f"record {i}: no provenance stamp")
            else:
                legacy += 1
            prev = claimed
        return {"ok": not errors, "records": n, "legacy_unstamped": legacy, "errors": errors[:50],
                "versions": [dict(zip(("code", "policy_version", "policy_hash", "prompt"), k.split("|")), records=c)
                             for k, c in versions.items()]}


PROV_KEYS = ("code", "policy_version", "policy_hash", "prompt", "provider", "model")


def main(argv: list[str] | None = None) -> int:
    """python -m log_on.audit verify [PATH ...] [--require-provenance]"""
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="python -m log_on.audit")
    ap.add_argument("cmd", choices=["verify"])
    ap.add_argument("paths", nargs="*", default=["runs/audit.jsonl"])
    ap.add_argument("--require-provenance", action="store_true", help="fail on any unstamped (pre-O-3) record")
    a = ap.parse_args(argv)
    rc = 0
    for p in a.paths:
        rep = AuditLog(p).verify_report(a.require_provenance)
        print(json.dumps({"path": p, **rep}, indent=2))
        rc |= 0 if rep["ok"] else 1
    sys.stdout.flush()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
