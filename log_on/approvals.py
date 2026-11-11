"""Pending-approval service shared by the CLI and the approval web UI (post-Phase-4 roadmap step 1).

The graph pauses at the `approval` node with a LangGraph `interrupt()`; the checkpointer persists the paused state.
This module turns those interrupts into reviewable items and applies decisions:

  * `list_pending()`  every thread whose next node is `approval`, as a PII-redacted preview
                      (goal, plan, tool risk tiers, step references, policy + permission decision, memory warnings)
  * `decide()`        approve / reject by a *named* approver with a note; refuses expired requests (and denies
                      them), refuses a stale form (the interrupt id must still be the pending one), then resumes the
                      graph via `Orchestrator.resume`, and writes the decision to:
                        - the run's hash-chained audit log (node `approval_decision`), and
                        - the hash-chained feedback log `runs/feedback.jsonl` (roadmap L-1: approver notes and deny
                          reasons as future learning data). Both are PII-redacted before writing.
  * `expire_stale()`  denies every request older than the TTL (approver `system:expiry`).

Expiry: LOG_ON_APPROVAL_TTL_S (default 86400 s = 24 h), measured from the checkpoint written when the run paused.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import permissions
from .audit import AuditLog
from .guards.validators import redact_text

DEFAULT_TTL_S = 24 * 3600
MACHINE_APPROVERS = {"", "auto", "auto-approve", "system", "bot", "learning", "unknown", "eval", "ci"}
EXPIRY_ACTOR = "system:expiry"
REF_RE = re.compile(r"\{\{\s*steps\[(\d+)\]\.output\s*\}\}")


class ApprovalError(RuntimeError):
    """A decision that must not be applied. `code` maps to an HTTP status in the web UI."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def ttl_seconds() -> int:
    try:
        return max(1, int(os.getenv("LOG_ON_APPROVAL_TTL_S", DEFAULT_TTL_S)))
    except ValueError:
        return DEFAULT_TTL_S


def mask_address(addr: str) -> str:
    """finance@acme.example -> f*****e@acme.example: the approver sees the domain (the safety-relevant part)
    and the shape of the mailbox, the preview does not reproduce the full address."""
    local, at, dom = str(addr or "").partition("@")
    if not at:
        return redact_text(str(addr or ""))
    m = local[0] + "*" * max(1, len(local) - 2) + local[-1] if len(local) > 2 else "*" * len(local)
    return f"{m}@{dom}"


def _domain_trusted(addr: str) -> bool:
    dom = str(addr or "").rpartition("@")[2].lower()
    return any(dom == t or dom.endswith("." + t) for t in permissions.trusted_domains())


def describe_step(i: int, step: dict, plan: list[dict], gated: set[int]) -> dict:
    """A human-readable, PII-redacted preview of one plan step."""
    tool = step.get("tool", "?")
    args = step.get("args") or {}
    pol = permissions.TOOL_POLICY.get(tool, {})
    shown: dict[str, str] = {}
    for k, v in args.items():
        s = str(v)
        refs = [int(n) for n in REF_RE.findall(s)]
        if refs and REF_RE.fullmatch(s.strip()):
            n = refs[0]
            src = plan[n]["tool"] if n < len(plan) else "?"
            shown[k] = f"[output of step {n + 1} ({src}), PII scrubbed on the way out]"
        elif k == permissions.EGRESS_ARGS.get(tool):
            shown[k] = mask_address(s)
        else:
            shown[k] = redact_text(s)
    effect = {
        "send_report": "Sends an email (mock outbox) — externally visible",
        "lookup_customer": "Reads a customer record (personal data; output is redacted)",
        "get_report": "Reads an internal report",
        "calculator": "Evaluates an arithmetic expression",
    }.get(tool, "Runs a read-only tool" if pol and not pol.get("approval") else "Runs a tool")
    out = {"index": i + 1, "tool": tool, "risk": pol.get("risk", "unknown"), "needs_approval": i in gated,
           "effect": effect, "args": shown}
    dest_arg = permissions.EGRESS_ARGS.get(tool)
    if dest_arg:
        out["destination"] = mask_address(args.get(dest_arg, ""))
        out["destination_trusted"] = _domain_trusted(args.get(dest_arg, ""))
    return out


@dataclass
class PendingApproval:
    run_id: str
    interrupt_id: str
    user: str
    goal: str                      # redacted
    requested_at: datetime
    expires_at: datetime
    planner: str
    steps: list[dict]
    policy: dict
    permission: dict
    memory_warnings: list[str] = field(default_factory=list)
    attempt: int | None = None

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at

    @property
    def seconds_left(self) -> int:
        return int((self.expires_at - datetime.now(timezone.utc)).total_seconds())

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "interrupt_id": self.interrupt_id, "user": self.user, "goal": self.goal,
                "requested_at": self.requested_at.isoformat(), "expires_at": self.expires_at.isoformat(),
                "expired": self.expired, "seconds_left": self.seconds_left, "planner": self.planner,
                "steps": self.steps, "policy": self.policy, "permission": self.permission,
                "memory_warnings": self.memory_warnings, "attempt": self.attempt}


class ApprovalService:
    def __init__(self, orch, feedback_path: str | os.PathLike | None = None, ttl_s: int | None = None):
        self.orch = orch
        self.ttl_s = ttl_s or ttl_seconds()
        path = feedback_path or Path(orch.audit.path).parent / "feedback.jsonl"
        self.feedback = AuditLog(path, redact=redact_text)
        self._lock = threading.Lock()

    # --- reading -------------------------------------------------------------------------------------
    def _thread_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for c in self.orch.checkpointer.list(None):
            tid = c.config["configurable"].get("thread_id")
            if tid:
                seen.setdefault(tid, None)
        return list(seen)

    def get(self, run_id: str) -> PendingApproval | None:
        snap = self.orch.graph.get_state({"configurable": {"thread_id": run_id}})
        if not snap or not snap.next or snap.next[0] != "approval":
            return None
        intr = next((i for t in snap.tasks if t.name == "approval" for i in t.interrupts), None)
        if intr is None:
            return None
        v = snap.values
        requested = datetime.fromisoformat(snap.created_at) if snap.created_at else datetime.now(timezone.utc)
        if requested.tzinfo is None:
            requested = requested.replace(tzinfo=timezone.utc)
        from datetime import timedelta
        plan = v.get("plan") or []
        perm = v.get("permission") or {}
        gated = set(perm.get("approval_steps") or [])
        pol = v.get("policy") or {}
        return PendingApproval(
            run_id=run_id, interrupt_id=intr.id, user=v.get("user", "?"), goal=redact_text(v.get("goal", "")),
            requested_at=requested, expires_at=requested + timedelta(seconds=self.ttl_s),
            planner=str(v.get("planner", "?")),
            steps=[describe_step(i, s, plan, gated) for i, s in enumerate(plan)],
            policy={"allowed": pol.get("allowed"), "rule_ids": pol.get("rule_ids", []),
                    "reasons": pol.get("reasons", []), "normalized": pol.get("normalized", False)},
            permission={"needs_approval": perm.get("needs_approval"), "approval_steps": [i + 1 for i in sorted(gated)],
                        "reason": "high-risk tool with an externally visible side effect" if gated else "",
                        "checks_passed": ["tool allow-list", "recipient typed by the user (provenance)",
                                          "personal data only to trusted domains", "plan step references valid"]},
            memory_warnings=[redact_text(w) for w in (intr.value or {}).get("memory_warnings", [])],
            attempt=v.get("attempt"))

    def list_pending(self) -> list[PendingApproval]:
        items = [p for p in (self.get(t) for t in self._thread_ids()) if p is not None]
        return sorted(items, key=lambda p: p.requested_at)

    def history(self, limit: int = 50) -> list[dict]:
        rows = [r for r in self.feedback.read() if r.get("event") == "decision"]
        return list(reversed(rows))[:limit]

    # --- writing -------------------------------------------------------------------------------------
    def decide(self, run_id: str, approve: bool, approver: str, note: str = "", interrupt_id: str | None = None,
               source: str = "web") -> dict:
        approver = (approver or "").strip()
        note = (note or "").strip()[:2000]
        if approver.lower() in MACHINE_APPROVERS or approver.lower().startswith("system"):
            raise ApprovalError("A named human approver is required.", 403)
        if not approve and not note:
            raise ApprovalError("A reason is required to reject (it is kept as feedback for learning).", 400)
        with self._lock:
            p = self.get(run_id)
            if p is None:
                raise ApprovalError(f"No pending approval for run {run_id} (already decided, or unknown).", 404)
            if interrupt_id and interrupt_id != p.interrupt_id:
                raise ApprovalError("This approval request changed since the page was loaded; reload and review again.", 409)
            if p.expired:
                self._apply(p, False, EXPIRY_ACTOR, f"expired after {self.ttl_s}s; {approver} tried to "
                            f"{'approve' if approve else 'reject'}", source, expired=True)
                raise ApprovalError("This approval request has expired and was denied automatically.", 410)
            return self._apply(p, approve, approver, note, source)

    def expire_stale(self) -> list[str]:
        done = []
        with self._lock:
            for p in self.list_pending():
                if p.expired:
                    self._apply(p, False, EXPIRY_ACTOR, f"expired after {self.ttl_s}s without a decision", "expiry",
                                expired=True)
                    done.append(p.run_id)
        return done

    def _apply(self, p: PendingApproval, approve: bool, approver: str, note: str, source: str,
               expired: bool = False) -> dict:
        age = int((datetime.now(timezone.utc) - p.requested_at).total_seconds())
        record = {"decision": "approve" if approve else "reject", "approver": approver, "note": note,
                  "source": source, "expired": expired, "interrupt_id": p.interrupt_id, "age_s": age,
                  "ttl_s": self.ttl_s}
        self.orch.audit.record(p.run_id, "approval_decision", "decision", record)
        res = self.orch.resume(p.run_id, approved=approve, approver=approver, note=note)
        self.feedback.record(p.run_id, "approval", "decision", {
            **record, "user": p.user, "goal": p.goal, "planner": p.planner,
            "steps": [{"tool": s["tool"], "args": s["args"], "risk": s["risk"]} for s in p.steps],
            "outcome": res.get("status")})
        return res
