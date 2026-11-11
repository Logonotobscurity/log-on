"""CLI with a durable SQLite checkpointer so a paused run can be approved from a *separate* process.

  python -m log_on "Send the Q3 revenue report to finance@acme.example"      # pauses, prints run id
  python -m log_on --resume <run_id> --approve                               # or --deny
  python -m log_on "Calculate 2*21" --auto-approve                            # approve inline
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from .demo import show
from .graph import Orchestrator


def main() -> None:
    ap = argparse.ArgumentParser(prog="log_on")
    ap.add_argument("goal", nargs="?")
    ap.add_argument("--resume", metavar="RUN_ID")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--approve", action="store_true")
    g.add_argument("--deny", action="store_true")
    ap.add_argument("--auto-approve", action="store_true", help="approve any pause immediately (testing only)")
    ap.add_argument("--runs-dir", default="runs")
    a = ap.parse_args()

    runs = Path(a.runs_dir); runs.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(runs / "checkpoints.sqlite", check_same_thread=False)
    orch = Orchestrator(audit_path=runs / "audit.jsonl", outbox=runs / "outbox.jsonl", checkpointer=SqliteSaver(conn))

    if a.resume:
        if not (a.approve or a.deny):
            ap.error("--resume needs --approve or --deny")
        res = orch.resume(a.resume, approved=a.approve)
    elif a.goal:
        res = orch.start(a.goal)
        if res["status"] == "awaiting_approval" and a.auto_approve:
            res = orch.resume(res["run_id"], approved=True, approver="auto-approve")
    else:
        ap.error("give a goal or --resume RUN_ID")
    print(f"run_id: {res['run_id']}")
    show("", res)
    if res["status"] == "awaiting_approval":
        print(f"\nApprove with: python -m log_on --resume {res['run_id']} --approve   (or --deny)")


if __name__ == "__main__":
    main()
