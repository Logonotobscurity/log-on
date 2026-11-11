"""Post-Phase-4 step 3: checkpointer selection (SQLite default, Postgres option), readiness endpoint, deploy files."""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from log_on.approval_ui import create_app
from log_on.approvals import ApprovalService
from log_on.checkpointing import make_checkpointer, ping, redact_dsn
from log_on.graph import Orchestrator
from log_on.planner import RuleBasedPlanner

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-token-not-a-secret-0001"  # fake, test-only
SEND = "Send the Q3 revenue report to finance@acme.example"
PG_DSN = os.getenv("LOG_ON_TEST_POSTGRES_DSN")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="set LOG_ON_TEST_POSTGRES_DSN=postgresql://... to test Postgres")


def _orch(tmp_path, saver):
    return Orchestrator(planner=RuleBasedPlanner(), audit_path=tmp_path / "audit.jsonl",
                        outbox=tmp_path / "outbox.jsonl", checkpointer=saver, tracer=False, quota=False)


def test_default_checkpointer_is_sqlite(tmp_path, monkeypatch):
    monkeypatch.delenv("LOG_ON_CHECKPOINTER", raising=False)
    s = make_checkpointer(tmp_path)
    assert type(s).__name__ == "SqliteSaver" and (tmp_path / "checkpoints.sqlite").exists() and ping(s)[0]


def test_bad_checkpointer_settings_fail_loudly(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        make_checkpointer(tmp_path, kind="mongo")
    monkeypatch.delenv("LOG_ON_POSTGRES_DSN", raising=False)
    with pytest.raises(RuntimeError, match="LOG_ON_POSTGRES_DSN"):
        make_checkpointer(tmp_path, kind="postgres")
    assert redact_dsn("postgresql://logon:s3cret@db:5432/logon") == "postgresql://logon:***@db:5432/logon"


def test_cli_builder_uses_the_env_selection(tmp_path, monkeypatch):
    from log_on.__main__ import build_orchestrator
    monkeypatch.setenv("LOG_ON_CHECKPOINTER", "memory")
    assert type(build_orchestrator(tmp_path, phase1=True).checkpointer).__name__ == "InMemorySaver"


@needs_pg
def test_postgres_checkpointer_pause_in_one_process_resume_in_another(tmp_path):
    a = _orch(tmp_path, make_checkpointer(tmp_path, kind="postgres", dsn=PG_DSN))
    run = a.start(SEND, run_id="pg-" + uuid.uuid4().hex[:8])
    assert run["status"] == "awaiting_approval"
    b = _orch(tmp_path, make_checkpointer(tmp_path, kind="postgres", dsn=PG_DSN))  # e.g. the approval UI container
    assert b.pending_node(run["run_id"]) == "approval"
    assert run["run_id"] in [p.run_id for p in ApprovalService(b).list_pending()]
    done = ApprovalService(b).decide(run["run_id"], True, "ada", "ok", source="test")
    assert done["status"] == "completed" and b.pending_node(run["run_id"]) is None
    assert ping(b.checkpointer) == (True, "PostgresSaver")


@needs_pg
def test_quota_store_on_postgres(tmp_path):
    from log_on.quotas import Limits, UserQuota
    q = UserQuota(Limits(runs=1), dsn=PG_DSN)
    user = "u-" + uuid.uuid4().hex[:8]
    assert q.start_run(user, user + "-1")["allowed"] and not q.start_run(user, user + "-2")["allowed"]
    q.record(user, user + "-1", {"total_tokens": 42, "est_cost_usd": 0.001})
    assert q.used(user) == {"runs": 1, "tokens": 42, "cost_usd": 0.001}


def test_readyz_and_healthz(tmp_path):
    from log_on.checkpointing import make_checkpointer as mk
    svc = ApprovalService(_orch(tmp_path, mk(tmp_path, kind="sqlite")))
    svc.orch.start("Calculate 2 * 21")
    c = TestClient(create_app(svc, tokens={TOKEN: "ada"}))
    assert c.get("/healthz").json() == {"ok": True}
    r = c.get("/readyz")  # no auth needed, no personal data
    body = r.json()
    assert r.status_code == 200 and body["ready"] and body["checks"]["checkpointer"]["ok"] and body["policy_version"]
    assert "goal" not in r.text and "finance" not in r.text

    class Broken:
        def list(self, *a, **k):
            raise ConnectionError("db down")
    svc.orch.checkpointer = Broken()
    r = c.get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["checkpointer"] == {"ok": False, "backend": "sqlite",
                                                                          "detail": "Broken: ConnectionError"}


# ---------------- deploy files (static checks; `docker build` is not available on the dev box) ----------------
def test_dockerfile_is_multistage_nonroot_with_optional_ml():
    d = (ROOT / "Dockerfile").read_text()
    assert len(re.findall(r"(?m)^FROM ", d)) >= 2 and re.search(r"(?m)^USER \d+|^USER app", d)
    assert "ARG WITH_ML" in d and "requirements-ml.txt" in d and "HEALTHCHECK" in d
    ignore = (ROOT / ".dockerignore").read_text().split()
    for must in (".venv", "runs", "*.gguf", "*.onnx", ".env", ".git", "models"):
        assert any(line.strip("/") in (must, "**/" + must) or line == "**/" + must for line in ignore), must


def test_compose_has_services_and_no_literal_secrets():
    c = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert {"app", "approval-ui", "postgres", "llamacpp"} <= set(c["services"])
    assert c["services"]["llamacpp"].get("profiles") == ["llm"]
    text = (ROOT / "docker-compose.yml").read_text()
    for m in re.finditer(r"(?m)^\s*\w*(PASSWORD|TOKENS?)\w*:\s*(\S+)", text):
        assert m.group(2).startswith(('"${', "${")), m.group(0)  # only env placeholders
    for m in re.finditer(r"DSN:\s*(.+)", text):  # the DSN's password part is a placeholder too
        assert re.search(r"\}:\$\{POSTGRES_PASSWORD[^@]*\}@", m.group(1)), m.group(0)
    env_example = (ROOT / ".env.example").read_text()
    assert "LOG_ON_APPROVER_TOKENS=" in env_example and "POSTGRES_PASSWORD=" in env_example
    assert "127.0.0.1:" in text  # ports published on localhost only
