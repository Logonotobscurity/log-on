"""python -m log_on.learning {optimize|list|show|approve|reject|rollback}

  optimize --provider llamacpp [--base-url URL] [--model M] [--optimizer bootstrap|mipro] [--augment 40]
           [--no-traj] [--report docs/dspy_report_phase4.json] [--verifier]
  list [--verify]                   versions, status, scores, which one is active
  approve VERSION --by NAME [--note TEXT]   human promotion (gate re-checked)
  reject VERSION --by NAME [--note TEXT]
  rollback --by NAME                deactivate the active planner prompt (falls back to unoptimised / rules)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .registry import PromotionRefused, PromptRegistry


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m log_on.learning")
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("optimize")
    o.add_argument("--provider", default="llamacpp")
    o.add_argument("--base-url")
    o.add_argument("--model")
    o.add_argument("--optimizer", default="bootstrap", choices=["bootstrap", "mipro"])
    o.add_argument("--augment", type=int, default=40)
    o.add_argument("--demos", type=int, default=4)
    o.add_argument("--no-traj", action="store_true")
    o.add_argument("--no-propose", action="store_true")
    o.add_argument("--verifier", action="store_true", help="also optimise the verifier program (report only)")
    o.add_argument("--report")
    ls = sub.add_parser("list")
    ls.add_argument("--verify", action="store_true", help="exit 1 on a sha256 mismatch or a broken approvals chain")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("version")
        p.add_argument("--by", required=True)
        p.add_argument("--note", default="")
        p.add_argument("--component", default="planner")
    r = sub.add_parser("rollback")
    r.add_argument("--by", required=True)
    r.add_argument("--note", default="")
    r.add_argument("--component", default="planner")
    a = ap.parse_args(argv)
    reg = PromptRegistry()

    if a.cmd == "optimize":
        import os
        os.environ.setdefault("LOG_ON_AS_OF", "2026-11-11")
        from ..serving import make_provider
        from .optimize import run, run_verifier
        from .signatures import to_dspy_lm
        prov = make_provider(a.provider, base_url=a.base_url, model=a.model, timeout=300)
        lm = to_dspy_lm(prov)
        rep = run(lm, model_label=f"{prov.name}:{prov.model}", optimizer=a.optimizer, n_aug=a.augment,
                  max_demos=a.demos, registry=reg, traj=not a.no_traj, propose=not a.no_propose)
        if a.verifier:
            v = run_verifier(lm, max_demos=a.demos)
            rep["verifier"] = {k: v[k] for k in ("scores", "train", "dev")}
        if a.report:
            Path(a.report).write_text(json.dumps(rep, indent=2, default=str))
        return 0
    if a.cmd == "list":
        data = json.loads(reg.index_path.read_text()) if reg.index_path.exists() else {"components": {}}
        for comp, c in data["components"].items():
            print(f"{comp}: active = {c.get('active')}")
            for ver, v in c["versions"].items():
                s = v.get("scores", {})
                print(f"  {ver:<28} {v['status']:<9} model={v.get('model')} dev {s.get('baseline')} -> "
                      f"{s.get('optimized')}  traj {s.get('trajectory_before')} -> {s.get('trajectory_after')}  "
                      f"gate={'pass' if (v.get('eval_gate') or {}).get('passed') else 'FAIL'}")
        if a.verify:
            problems = reg.verify()
            for p in problems:
                print("INTEGRITY:", p)
            print("registry integrity:", "FAILED" if problems else "ok")
            return 1 if problems else 0
        return 0
    try:
        if a.cmd == "approve":
            v = reg.approve(a.component, a.version, a.by, a.note)
            print(f"approved {a.component} {a.version} by {v['approved_by']}; now active")
        elif a.cmd == "reject":
            reg.reject(a.component, a.version, a.by, a.note)
            print(f"rejected {a.component} {a.version}")
        elif a.cmd == "rollback":
            print(f"rolled back {a.component}: {reg.rollback(a.component, a.by, a.note)} deactivated")
    except PromotionRefused as exc:
        print(f"REFUSED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
