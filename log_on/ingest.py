"""Intake CLI: PDF -> chunks JSON (with provenance) -> entities/relations -> graph store.

  python -m log_on.ingest samples/lagoon_freight_portfolio_fy2026.pdf                # Kuzu at runs/graph.kuzu
  LOG_ON_GRAPH_BACKEND=neo4j NEO4J_PASSWORD=... python -m log_on.ingest <pdf>        # Neo4j
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .docai import ingest_pdf
from .guards import redact_text
from .kg import get_extractor, load_into, make_graph_store
from .provenance import stamp


def run(pdf: str | Path, graph_path: str | Path | None = None, backend: str | None = None,
        chunks_out: str | Path | None = None, audit_path: str | Path | None = None, extractor=None) -> dict[str, Any]:
    doc = ingest_pdf(pdf)
    if chunks_out:
        doc.save(chunks_out)
    ex = (extractor or get_extractor()).extract(doc)
    with make_graph_store(backend, graph_path) as store:
        counts = load_into(store, ex)
        backend_name = store.backend
    summary = {"doc_id": doc.doc_id, "title": doc.title, "pages": doc.pages, "chunks": len(doc.chunks),
               "tables": len(doc.tables()), "backend": backend_name, "counts": counts, "warnings": ex.warnings,
               "chunks_file": str(chunks_out) if chunks_out else None}
    if audit_path:
        AuditLog(audit_path, redact=redact_text, provenance=stamp).record(doc.doc_id, "ingest", "completed", summary)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(prog="log_on.ingest")
    ap.add_argument("pdf")
    ap.add_argument("--graph", default=None, help="Kuzu database path (default runs/graph.kuzu)")
    ap.add_argument("--backend", default=None, help="kuzu (default) | neo4j")
    ap.add_argument("--chunks", default="runs/chunks.json")
    ap.add_argument("--audit", default="runs/audit.jsonl")
    a = ap.parse_args()
    print(json.dumps(run(a.pdf, a.graph, a.backend, a.chunks, a.audit), indent=2))


if __name__ == "__main__":
    main()
