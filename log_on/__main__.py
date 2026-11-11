"""CLI with a durable SQLite checkpointer so a paused run can be approved from a *separate* process.

  python -m log_on "Send the Q3 revenue report to finance@acme.example"      # pauses, prints run id
  python -m log_on --resume <run_id> --approve --by ada --note "checked"      # or --deny --note "reason"
  python -m log_on --pending                                                 # list paused approvals
  python -m log_on.approval_ui                                               # same, in a local web UI
  python -m log_on "Calculate 2*21" --auto-approve                            # approve inline

Phase 2 (default): long-term memory in runs/memory.sqlite and, if `python -m log_on.ingest <pdf>` has been
run, the knowledge graph at runs/graph.kuzu (or Neo4j with LOG_ON_GRAPH_BACKEND=neo4j). --phase1 disables both.

Phase 3 (default): self-checking loop + budgets + tracing (runs/traces.jsonl). --no-loop disables the loop.
A run that cannot be verified pauses for a human:
  python -m log_on --resume <run_id> --accept | --abort | --retry-goal "Look up customer C-1002"
Inspect any run's trace with:  python -m log_on.trace <run_id>   (or --list)
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from .demo import show
from .graph import Orchestrator


def build_orchestrator(runs_dir: str | Path = "runs", phase1: bool = False, no_loop: bool = False,
                       max_attempts: int | None = None) -> Orchestrator:
    """The CLI's orchestrator (durable SQLite checkpointer). The approval web UI builds the same one, so a run
    paused by either can be resumed by the other (use the same --phase1 / --no-loop flags for both)."""
    runs = Path(runs_dir); runs.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(runs / "checkpoints.sqlite", check_same_thread=False)
    extra = {}
    if not phase1:
        from .memory import MemoryManager, make_memory_store
        store, emb = make_memory_store(runs / "memory.sqlite")
        extra["memory"] = MemoryManager(store, emb)
        graph = Path(os.environ.get("LOG_ON_GRAPH_PATH", runs / "graph.kuzu"))
        if os.environ.get("LOG_ON_GRAPH_BACKEND", "kuzu") == "neo4j":
            extra["graph_backend"] = "neo4j"
        elif graph.exists():
            extra["graph_path"] = graph
    if not no_loop:
        from .loop import LoopConfig
        extra["loop"] = LoopConfig.from_env(**({"max_attempts": max_attempts} if max_attempts else {}))
    return Orchestrator(audit_path=runs / "audit.jsonl", outbox=runs / "outbox.jsonl", checkpointer=SqliteSaver(conn),
                        **extra)


def main() -> None:
    ap = argparse.ArgumentParser(prog="log_on")
    ap.add_argument("goal", nargs="?")
    ap.add_argument("--resume", metavar="RUN_ID")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--approve", action="store_true")
    g.add_argument("--deny", action="store_true")
    g.add_argument("--accept", action="store_true", help="escalation: accept the unverified result")
    g.add_argument("--abort", action="store_true", help="escalation: stop the run")
    g.add_argument("--retry-goal", metavar="GOAL", help="escalation: retry once with a revised goal")
    ap.add_argument("--by", default=os.environ.get("LOG_ON_APPROVER", "rhonda"), help="approver name (default "
                    "$LOG_ON_APPROVER or rhonda)")
    ap.add_argument("--note", default="", help="approval note / reason for --deny (stored as feedback)")
    ap.add_argument("--pending", action="store_true", help="list runs waiting for approval")
    ap.add_argument("--auto-approve", action="store_true", help="approve any pause immediately (testing only)")
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--user", default="rhonda", help="memory namespace (Phase 2)")
    ap.add_argument("--phase1", action="store_true", help="disable Phase 2 memory + knowledge graph")
    ap.add_argument("--no-loop", action="store_true", help="disable the Phase 3 verify/retry/escalate loop")
    ap.add_argument("--max-attempts", type=int, help="Phase 3: attempts before escalating (default 3)")
    a = ap.parse_args()

    runs = Path(a.runs_dir)
    orch = build_orchestrator(runs, a.phase1, a.no_loop, a.max_attempts)
    from .approvals import ApprovalError, ApprovalService
    svc = ApprovalService(orch)

    if a.pending:
        items = svc.list_pending()
        for p in items:
            state = "EXPIRED" if p.expired else f"expires in {p.seconds_left // 60} min"
            print(f"{p.run_id}  [{state}]  {p.goal}")
            for st in p.steps:
                print(f"    {'*' if st['needs_approval'] else ' '} {st['index']}. {st['tool']} {st['args']}")
        print(f"{len(items)} pending approval(s)")
        return

    if a.resume:
        if a.accept or a.abort or a.retry_goal:
            res = orch.respond(a.resume, "accept" if a.accept else "abort" if a.abort else "retry", goal=a.retry_goal)
        elif a.approve or a.deny:
            if orch.pending_node(a.resume) == "approval":
                try:  # same rules as the web UI: named approver, reason to deny, expiry, audit + feedback log
                    res = svc.decide(a.resume, a.approve, a.by, a.note or ("" if a.approve else "(no reason given via CLI)"),
                                     source="cli")
                except ApprovalError as e:
                    ap.exit(1, f"error: {e}\n")
            else:
                res = orch.resume(a.resume, approved=a.approve, approver=a.by, note=a.note)
        else:
            ap.error("--resume needs --approve/--deny (approval) or --accept/--abort/--retry-goal (escalation)")
    elif a.goal:
        res = orch.start(a.goal, user=a.user)
        if res["status"] == "awaiting_approval" and a.auto_approve:
            res = orch.resume(res["run_id"], approved=True, approver="auto-approve")
    else:
        ap.error("give a goal, --resume RUN_ID or --pending")
    print(f"run_id: {res['run_id']}")
    show("", res)
    if res["status"] == "awaiting_approval":
        print(f"\nApprove with: python -m log_on --resume {res['run_id']} --approve --by <name>   "
              f"(or --deny --note \"reason\"), or in the web UI: python -m log_on.approval_ui")
    if res["status"] == "awaiting_human":
        print(f"\nEscalated. Answer with: python -m log_on --resume {res['run_id']} --accept | --abort | "
              f"--retry-goal \"...\"")
    print(f"trace: python -m log_on.trace {res['run_id']} --file {runs / 'traces.jsonl'}")


if __name__ == "__main__":
    main()
