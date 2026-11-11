"""Approval web UI + shared approval service (post-Phase-4 roadmap step 1)."""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from starlette.testclient import TestClient

from log_on.approval_ui import COOKIE, create_app, load_tokens
from log_on.approvals import EXPIRY_ACTOR, ApprovalError, ApprovalService, mask_address
from log_on.audit import AuditLog
from log_on.graph import Orchestrator
from log_on.planner import RuleBasedPlanner

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-token-not-a-secret-0001"      # fake, test-only
OTHER = "test-token-not-a-secret-0002"
SEND = "Send the Q3 revenue report to finance@acme.example"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_orch(tmp_path):
    conn = sqlite3.connect(tmp_path / "checkpoints.sqlite", check_same_thread=False)
    return Orchestrator(planner=RuleBasedPlanner(), audit_path=tmp_path / "audit.jsonl",
                        outbox=tmp_path / "outbox.jsonl", checkpointer=SqliteSaver(conn), tracer=False)


@pytest.fixture
def svc(tmp_path):
    return ApprovalService(make_orch(tmp_path))


@pytest.fixture
def client(svc):
    return TestClient(create_app(svc, tokens={TOKEN: "ada", OTHER: "bayo"}))


def outbox(svc):
    p = Path(svc.orch.outbox)
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_pending_preview_is_redacted_and_explains_the_action(svc):
    run = svc.orch.start(SEND + ". My NIN is 12345678901, call 08000000001")
    assert run["status"] == "awaiting_approval"
    [p] = svc.list_pending()
    assert p.run_id == run["run_id"] and not p.expired and p.seconds_left > 3600
    blob = json.dumps(p.to_dict())
    assert "12345678901" not in blob and "08000000001" not in blob and "finance@acme.example" not in blob
    send = next(s for s in p.steps if s["tool"] == "send_report")
    assert send["needs_approval"] and send["risk"] == "high" and send["destination"] == mask_address("finance@acme.example")
    assert send["destination_trusted"] is True and "output of step 1 (get_report)" in send["args"]["body"]
    assert p.permission["approval_steps"] == [2] and p.policy["allowed"] is True


def test_auth_required_and_tokens_validated(svc, client):
    assert client.get("/api/approvals").status_code == 401
    assert client.get("/api/approvals", headers={"Authorization": "Bearer wrong-token-wrong-token"}).status_code == 401
    assert client.post("/api/approvals/x/decide", json={"decision": "approve"}).status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    with pytest.raises(RuntimeError):
        create_app(svc, tokens={})
    with pytest.raises(ValueError):
        load_tokens("ada:short")
    assert load_tokens(f"ada:{TOKEN}, bayo:{OTHER}") == {TOKEN: "ada", OTHER: "bayo"}
    assert client.get("/healthz").json() == {"ok": True}


def test_api_approve_resumes_graph_and_writes_audit_and_feedback(svc, client):
    run_id = svc.orch.start(SEND)["run_id"]
    lst = client.get("/api/approvals", headers=AUTH).json()
    assert lst["approver"] == "ada" and [p["run_id"] for p in lst["pending"]] == [run_id]
    r = client.post(f"/api/approvals/{run_id}/decide", headers=AUTH,
                    json={"decision": "approve", "note": "matches the monthly schedule",
                          "interrupt_id": lst["pending"][0]["interrupt_id"]})
    assert r.status_code == 200 and r.json()["status"] == "completed" and r.json()["approver"] == "ada"
    assert len(outbox(svc)) == 1 and svc.list_pending() == []
    audit = svc.orch.audit
    assert audit.verify()
    dec = [x for x in audit.read(run_id) if x["node"] == "approval_decision"]
    assert dec and dec[0]["data"]["approver"] == "ada" and dec[0]["data"]["source"] == "api"
    appr = [x for x in audit.read(run_id) if x["node"] == "approval" and x["event"] == "completed"]
    assert appr[0]["data"]["approval"] == {"approved": True, "approver": "ada", "note": "matches the monthly schedule"}
    fb = AuditLog(svc.feedback.path)
    assert fb.verify()
    [row] = [x for x in fb.read() if x["event"] == "decision"]
    assert row["data"]["note"] == "matches the monthly schedule" and row["data"]["outcome"] == "completed"
    # deciding twice is refused
    again = client.post(f"/api/approvals/{run_id}/decide", headers=AUTH, json={"decision": "approve"})
    assert again.status_code == 404


def test_reject_needs_a_reason_and_stores_it_as_feedback(svc, client):
    run_id = svc.orch.start(SEND)["run_id"]
    r = client.post(f"/api/approvals/{run_id}/decide", headers=AUTH, json={"decision": "reject"})
    assert r.status_code == 400 and "reason" in r.json()["error"]
    r = client.post(f"/api/approvals/{run_id}/decide", headers=AUTH,
                    json={"decision": "reject", "note": "finance already has it; reach me on 08000000001"})
    assert r.status_code == 200 and r.json()["status"] == "denied"
    assert outbox(svc) == []
    [row] = svc.history()
    assert row["data"]["decision"] == "reject" and row["data"]["approver"] == "ada"
    assert "finance already has it" in row["data"]["note"] and "08000000001" not in row["data"]["note"]  # redacted


def test_expired_request_cannot_be_approved_and_is_denied(tmp_path):
    svc = ApprovalService(make_orch(tmp_path), ttl_s=1)
    run_id = svc.orch.start(SEND)["run_id"]
    time.sleep(1.2)
    assert svc.get(run_id).expired
    with pytest.raises(ApprovalError) as e:
        svc.decide(run_id, True, "ada", "late")
    assert e.value.code == 410
    assert svc.get(run_id) is None and outbox(svc) == []
    appr = [x for x in svc.orch.audit.read(run_id) if x["node"] == "approval" and x["event"] == "completed"]
    assert appr[0]["data"]["approval"]["approver"] == EXPIRY_ACTOR
    assert svc.history()[0]["data"]["expired"] is True


def test_expired_requests_are_swept_when_listing(tmp_path):
    svc = ApprovalService(make_orch(tmp_path), ttl_s=1)
    run_id = svc.orch.start(SEND)["run_id"]
    time.sleep(1.2)
    c = TestClient(create_app(svc, tokens={TOKEN: "ada"}))
    body = c.get("/api/approvals", headers=AUTH).json()
    assert body["expired_now"] == [run_id] and body["pending"] == []
    assert outbox(svc) == []


def test_stale_form_and_machine_approvers_are_refused(svc):
    run_id = svc.orch.start(SEND)["run_id"]
    with pytest.raises(ApprovalError) as e:
        svc.decide(run_id, True, "ada", interrupt_id="not-the-current-interrupt")
    assert e.value.code == 409
    for bot in ("", "auto-approve", "system:cron", "eval"):
        with pytest.raises(ApprovalError) as e:
            svc.decide(run_id, True, bot)
        assert e.value.code == 403
    assert svc.get(run_id) is not None  # still pending, nothing ran


def test_browser_flow_login_csrf_detail_and_decide(svc, client):
    run_id = svc.orch.start(SEND)["run_id"]
    assert "Unknown approver token" in client.post("/login", data={"token": "nope"}).text
    r = client.post("/login", data={"token": OTHER}, follow_redirects=False)
    assert r.status_code == 303 and COOKIE in r.headers["set-cookie"]
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    home = client.get("/")
    assert run_id in home.text and "bayo" in home.text and "default-src 'none'" in home.headers["content-security-policy"]
    page = client.get(f"/approvals/{run_id}").text
    assert "send_report" in page and "trusted domain" in page and "finance@acme.example" not in page
    assert "f*****e@acme.example" in page
    iid = re.search(r'name="interrupt_id" value="([^"]+)"', page).group(1)
    bad = client.post(f"/approvals/{run_id}/decide", data={"decision": "approve", "csrf": "x", "interrupt_id": iid})
    assert "CSRF" in bad.text and svc.get(run_id) is not None
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    done = client.post(f"/approvals/{run_id}/decide", data={"decision": "approve", "csrf": csrf, "interrupt_id": iid,
                                                             "note": "ok"}, follow_redirects=False)
    assert done.status_code == 303 and svc.get(run_id) is None and len(outbox(svc)) == 1
    hist = client.get("/history").text
    assert "bayo" in hist and "approve" in hist and "chain ok" in hist


def test_cli_pending_and_named_approval_still_work(tmp_path):
    env = {**os.environ, "LOG_ON_LLM_PROVIDER": "none", "LOG_ON_TRACE_EXPORTERS": "none", "PYTHONPATH": str(ROOT)}
    base = [sys.executable, "-m", "log_on", "--runs-dir", str(tmp_path), "--phase1", "--no-loop"]
    out = subprocess.run(base + [SEND], capture_output=True, text=True, env=env, cwd=ROOT, timeout=120).stdout
    run_id = re.search(r"run_id: (\w+)", out).group(1)
    pend = subprocess.run(base + ["--pending"], capture_output=True, text=True, env=env, cwd=ROOT, timeout=120).stdout
    assert run_id in pend and "1 pending approval(s)" in pend
    res = subprocess.run(base + ["--resume", run_id, "--approve", "--by", "ada", "--note", "cli ok"],
                         capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
    assert "status      : completed" in res.stdout, res.stderr
    fb = AuditLog(tmp_path / "feedback.jsonl").read()
    assert fb[-1]["data"]["source"] == "cli" and fb[-1]["data"]["approver"] == "ada"


def test_no_approver_tokens_committed():
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], capture_output=True, text=True,
                           cwd=ROOT).stdout.split()
    pat = re.compile(r"LOG_ON_APPROVER_TOKENS\s*=\s*[\"']?\w+:(?!\$\(|<|\.\.\.|token\b)[A-Za-z0-9_\-]{16,}")
    hits = [f for f in files if f.endswith((".py", ".md", ".yml", ".yaml", ".txt", ".sh", ".env", ".json"))
            and (ROOT / f).is_file() and pat.search((ROOT / f).read_text(errors="ignore"))]
    assert hits == []
