"""Checkpointer selection (post-Phase-4 step 3, ticket D-2).

    LOG_ON_CHECKPOINTER = sqlite (default) | postgres | memory
    LOG_ON_POSTGRES_DSN  = postgresql://user:password@host:5432/db   (env only; never written to disk or logs)

SQLite keeps paused runs in <runs>/checkpoints.sqlite (single host). Postgres (langgraph-checkpoint-postgres, psycopg 3
connection pool) lets several app / approval-UI processes or containers share paused runs. Install the extra with
`pip install -r requirements-prod.txt`; the tables are created on first use (`PostgresSaver.setup()`).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def backend() -> str:
    return os.getenv("LOG_ON_CHECKPOINTER", "sqlite").strip().lower()


def postgres_dsn() -> str | None:
    return os.getenv("LOG_ON_POSTGRES_DSN") or None


def redact_dsn(dsn: str) -> str:
    """postgresql://user:secret@host/db -> postgresql://user:***@host/db (for messages)."""
    import re
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", dsn or "")


def make_checkpointer(runs_dir: str | Path = "runs", kind: str | None = None, dsn: str | None = None):
    kind = (kind or backend())
    if kind == "memory":
        from langgraph.checkpoint.memory import InMemorySaver
        return InMemorySaver()
    if kind == "postgres":
        dsn = dsn or postgres_dsn()
        if not dsn:
            raise RuntimeError("LOG_ON_CHECKPOINTER=postgres needs LOG_ON_POSTGRES_DSN")
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as e:  # pragma: no cover - depends on optional extras
            raise RuntimeError("Postgres checkpointer needs: pip install -r requirements-prod.txt") from e
        pool = ConnectionPool(dsn, min_size=1, max_size=int(os.getenv("LOG_ON_POSTGRES_POOL", "5")), open=True,
                              kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
        saver = PostgresSaver(pool)
        saver.setup()  # idempotent migrations
        return saver
    if kind != "sqlite":
        raise ValueError(f"LOG_ON_CHECKPOINTER={kind!r}: use sqlite, postgres or memory")
    from langgraph.checkpoint.sqlite import SqliteSaver
    runs = Path(runs_dir); runs.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(runs / "checkpoints.sqlite", check_same_thread=False))


def ping(saver) -> tuple[bool, str]:
    """Readiness: can we read from the checkpointer?"""
    try:
        next(iter(saver.list(None, limit=1)), None)
        return True, type(saver).__name__
    except Exception as e:  # noqa: BLE001
        return False, f"{type(saver).__name__}: {type(e).__name__}"
