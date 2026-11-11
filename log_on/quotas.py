"""Per-user, per-day budgets (post-Phase-4 step 3). Phase 3 budgets are per *run*; these cap what one user can use
in a day across runs, and are enforced by the graph:

* at intake (before anything else runs): a new run is refused if the user has no runs / tokens / cost left today;
* before every LLM-backed step (planning, replan, and verification when the verifier is an LLM): the run stops if
  today's usage plus this run's usage so far has reached a limit.

A stop ends the run with status `quota_exceeded`, a clear message, and a `quota_stop` audit record (which carries the
limits, today's usage and the reason). Usage is persisted per run (upsert, so pauses/resumes are counted once).

Environment (all optional; with no limit set the feature is off):
  LOG_ON_USER_DAILY_RUNS, LOG_ON_USER_DAILY_TOKENS, LOG_ON_USER_DAILY_COST_USD   defaults for every user
  LOG_ON_USER_BUDGETS   per-user overrides, e.g. "ada:runs=50,tokens=200000,cost=1;guest:runs=3" (0 = unlimited)
  LOG_ON_QUOTA_DB       SQLite file (default <runs dir>/quota.sqlite)
  LOG_ON_QUOTA_DSN      Postgres DSN instead of SQLite (defaults to LOG_ON_POSTGRES_DSN when LOG_ON_CHECKPOINTER=postgres)
  LOG_ON_QUOTA_TZ       time zone that defines "a day" (default Africa/Lagos)
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

KEYS = ("runs", "tokens", "cost")
_ENV = {"runs": "LOG_ON_USER_DAILY_RUNS", "tokens": "LOG_ON_USER_DAILY_TOKENS", "cost": "LOG_ON_USER_DAILY_COST_USD"}


def _num(v: str | None, cast):
    try:
        return cast(v) if v not in (None, "") else 0
    except ValueError:
        raise ValueError(f"bad budget value {v!r}") from None


def parse_overrides(spec: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for part in filter(None, (p.strip() for p in (spec or "").split(";"))):
        user, _, rest = part.partition(":")
        lim = {}
        for kv in filter(None, (x.strip() for x in rest.split(","))):
            k, _, v = kv.partition("=")
            k = k.strip().lower()
            if k not in KEYS:
                raise ValueError(f"LOG_ON_USER_BUDGETS: unknown limit {k!r} (use runs, tokens, cost)")
            lim[k] = _num(v.strip(), float if k == "cost" else int)
        out[user.strip()] = lim
    return out


@dataclass
class Limits:
    runs: int = 0       # 0 = unlimited
    tokens: int = 0
    cost: float = 0.0

    def as_dict(self) -> dict:
        return {"runs": self.runs, "tokens": self.tokens, "cost_usd": self.cost}


class _Store:
    """Tiny SQL layer: SQLite (default) or Postgres (psycopg), same schema."""

    def __init__(self, sqlite_path: Path | None = None, dsn: str | None = None):
        self.dsn, self._lock = dsn, threading.Lock()
        if dsn:
            import psycopg
            self.conn = psycopg.connect(dsn, autocommit=True)
            self.ph = "%s"
        else:
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(sqlite_path, check_same_thread=False, isolation_level=None, timeout=30)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.ph = "?"
        self.conn.execute("CREATE TABLE IF NOT EXISTS log_on_usage (run_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
                          "day TEXT NOT NULL, tokens BIGINT NOT NULL DEFAULT 0, cost DOUBLE PRECISION NOT NULL DEFAULT 0, "
                          "updated TEXT NOT NULL)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS log_on_usage_user_day ON log_on_usage (user_id, day)")

    def q(self, sql: str) -> str:
        return sql.replace("?", self.ph)

    def tx(self):
        """Serialise check-then-insert across threads and processes."""
        store = self

        class _Tx:
            def __enter__(self):
                store._lock.acquire()
                if store.dsn:
                    self.t = store.conn.transaction(); self.t.__enter__()
                    store.conn.execute("SELECT pg_advisory_xact_lock(4206001)")
                else:
                    store.conn.execute("BEGIN IMMEDIATE")
                return store

            def __exit__(self, et, ev, tb):
                try:
                    if store.dsn:
                        self.t.__exit__(et, ev, tb)
                    else:
                        store.conn.execute("ROLLBACK" if et else "COMMIT")
                finally:
                    store._lock.release()
        return _Tx()


class UserQuota:
    def __init__(self, defaults: Limits, overrides: dict[str, dict] | None = None, sqlite_path: Path | None = None,
                 dsn: str | None = None, tz: str = "Africa/Lagos"):
        self.defaults, self.overrides, self.tz = defaults, overrides or {}, ZoneInfo(tz)
        self.store = _Store(sqlite_path or Path("runs/quota.sqlite"), dsn)

    @classmethod
    def from_env(cls, runs_dir: str | Path = "runs") -> "UserQuota | None":
        d = Limits(runs=_num(os.getenv(_ENV["runs"]), int), tokens=_num(os.getenv(_ENV["tokens"]), int),
                   cost=_num(os.getenv(_ENV["cost"]), float))
        ov = parse_overrides(os.getenv("LOG_ON_USER_BUDGETS", ""))
        if not (d.runs or d.tokens or d.cost or any(any(v.values()) for v in ov.values())):
            return None  # feature off
        dsn = os.getenv("LOG_ON_QUOTA_DSN") or (os.getenv("LOG_ON_POSTGRES_DSN") if
                                                os.getenv("LOG_ON_CHECKPOINTER", "").lower() == "postgres" else None)
        path = Path(os.getenv("LOG_ON_QUOTA_DB") or Path(runs_dir) / "quota.sqlite")
        return cls(d, ov, path, dsn, os.getenv("LOG_ON_QUOTA_TZ", "Africa/Lagos"))

    def limits(self, user: str) -> Limits:
        o = self.overrides.get(user, {})
        return Limits(runs=int(o.get("runs", self.defaults.runs)), tokens=int(o.get("tokens", self.defaults.tokens)),
                      cost=float(o.get("cost", self.defaults.cost)))

    def today(self) -> str:
        return datetime.now(self.tz).date().isoformat()

    def _used(self, s: _Store, user: str, exclude_run: str | None = None) -> dict:
        row = s.conn.execute(s.q("SELECT COUNT(*), COALESCE(SUM(tokens), 0), COALESCE(SUM(cost), 0) FROM log_on_usage "
                                 "WHERE user_id = ? AND day = ? AND run_id <> ?"),
                             (user, self.today(), exclude_run or "")).fetchone()
        return {"runs": int(row[0]), "tokens": int(row[1]), "cost_usd": round(float(row[2]), 6)}

    def used(self, user: str) -> dict:
        return self._used(self.store, user)

    @staticmethod
    def _reasons(lim: Limits, used: dict, new_run: bool) -> list[str]:
        out = []
        if lim.runs and used["runs"] + (1 if new_run else 0) > lim.runs:
            out.append(f"runs {used['runs']}/{lim.runs}")
        if lim.tokens and used["tokens"] >= lim.tokens:
            out.append(f"tokens {used['tokens']}/{lim.tokens}")
        if lim.cost and used["cost_usd"] >= lim.cost:
            out.append(f"cost ${used['cost_usd']:.4f}/${lim.cost}")
        return out

    def start_run(self, user: str, run_id: str) -> dict:
        """Atomically check and count a new run. Returns {"allowed", "exceeded", "limits", "used", ...}."""
        lim = self.limits(user)
        with self.store.tx() as s:
            used = self._used(s, user, exclude_run=run_id)
            why = self._reasons(lim, used, new_run=True)
            if not why:
                s.conn.execute(s.q("INSERT INTO log_on_usage (run_id, user_id, day, tokens, cost, updated) "
                                   "VALUES (?, ?, ?, 0, 0, ?) ON CONFLICT (run_id) DO NOTHING"),
                               (run_id, user, self.today(), datetime.now(timezone.utc).isoformat()))
        return self._decision(user, lim, used, why)

    def check(self, user: str, run_id: str, run_usage: dict | None = None) -> dict:
        """Before an LLM-backed step: today's other runs + this run so far."""
        lim = self.limits(user)
        used = self._used(self.store, user, exclude_run=run_id)
        u = run_usage or {}
        used = {"runs": used["runs"] + 1, "tokens": used["tokens"] + int(u.get("total_tokens", 0)),
                "cost_usd": round(used["cost_usd"] + float(u.get("est_cost_usd", 0.0)), 6)}
        return self._decision(user, lim, used, self._reasons(lim, used, new_run=False))

    def record(self, user: str, run_id: str, usage: dict | None) -> None:
        if not usage:
            return
        s = self.store
        with s._lock:
            # UPDATE only: the row exists iff start_run admitted the run (a refused run is never counted)
            s.conn.execute(s.q("UPDATE log_on_usage SET tokens = ?, cost = ?, updated = ? WHERE run_id = ? AND user_id = ?"),
                           (int(usage.get("total_tokens", 0)), float(usage.get("est_cost_usd", 0.0)),
                            datetime.now(timezone.utc).isoformat(), run_id, user))

    def _decision(self, user, lim: Limits, used: dict, why: list[str]) -> dict:
        return {"allowed": not why, "user": user, "day": self.today(), "tz": str(self.tz), "limits": lim.as_dict(),
                "used": used, "exceeded": why}

    def message(self, d: dict) -> str:
        return (f"Stopped: the daily budget for user '{d['user']}' is used up ({'; '.join(d['exceeded'])}) for "
                f"{d['day']} ({d['tz']}). Nothing further was run. It resets at midnight, or an admin can raise "
                "LOG_ON_USER_BUDGETS.")
