"""python -m log_on.redteam [--planner rule|llm|dspy] [--provider P --base-url U --model M] [--gate]
                           [--only-category C] [--out report.json] [--md report.md] [--export-cases PATH]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .attacks import generate
from .harness import export_cases, run, to_markdown


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m log_on.redteam")
    ap.add_argument("--planner", default="rule", choices=["rule", "llm", "dspy"])
    ap.add_argument("--provider", default="llamacpp")
    ap.add_argument("--base-url")
    ap.add_argument("--model")
    ap.add_argument("--only-category")
    ap.add_argument("--gate", action="store_true", help="exit 1 if any attack succeeds")
    ap.add_argument("--out")
    ap.add_argument("--md")
    ap.add_argument("--export-cases")
    a = ap.parse_args(argv)
    if a.export_cases:  # freeze regression cases from the regex-only layer: the gate must not depend on ML extras
        import os
        os.environ.update({"LOG_ON_PII_BACKEND": "regex", "LOG_ON_INJECTION_ML": "off"})
        print("export: running in regex-only mode (LOG_ON_PII_BACKEND=regex, LOG_ON_INJECTION_ML=off)", flush=True)
    attacks = generate()
    if a.only_category:
        attacks = [x for x in attacks if x["category"].startswith(a.only_category)]
    factory, title = None, "rule-based planner"
    if a.planner != "rule":
        from ..serving import make_provider
        prov = make_provider(a.provider, base_url=a.base_url, model=a.model, timeout=300)
        title = f"{a.planner} planner on {prov.name}:{prov.model}"
        if a.planner == "llm":
            from ..planner import LLMPlanner
            factory = lambda: LLMPlanner(provider=prov)  # noqa: E731
        else:
            from ..learning.planner import DSPyPlanner
            factory = lambda: DSPyPlanner.from_registry(provider=prov)  # noqa: E731
    rep = run(attacks, factory, log=lambda m: print(m, flush=True))
    rep["planner"] = title
    md = to_markdown(rep, title)
    print(md)
    if a.out:
        Path(a.out).write_text(json.dumps(rep, indent=2, ensure_ascii=False))
    if a.md:
        Path(a.md).write_text(md + "\n")
    if a.export_cases:
        n = export_cases(attacks, rep["results"], Path(a.export_cases))
        print(f"exported {n} regression cases to {a.export_cases}")
    if a.gate and rep["summary"]["vulnerable"]:
        print(f"RED-TEAM GATE FAILED: {rep['summary']['vulnerable']} successful attack(s)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
