import pytest

from log_on.graph import Orchestrator
from log_on.planner import RuleBasedPlanner


@pytest.fixture
def orch(tmp_path):
    return Orchestrator(planner=RuleBasedPlanner(), audit_path=tmp_path / "audit.jsonl",
                        outbox=tmp_path / "outbox.jsonl")


# ---------------------------------------------------------------- Phase 2 fixtures
import os
from datetime import date
from pathlib import Path

SAMPLE_PDF = Path(__file__).resolve().parent.parent / "samples" / "lagoon_freight_portfolio_fy2026.pdf"
AS_OF = date(2026, 11, 11)  # "Q1" therefore means Q1 2027


@pytest.fixture(scope="session")
def sample_doc():
    from log_on.docai import ingest_pdf
    return ingest_pdf(SAMPLE_PDF)


@pytest.fixture(scope="session")
def extraction(sample_doc):
    from log_on.kg import RuleBasedExtractor
    return RuleBasedExtractor().extract(sample_doc)


@pytest.fixture(scope="session")
def kuzu_path(tmp_path_factory, extraction):
    from log_on.kg import KuzuGraphStore, load_into
    path = tmp_path_factory.mktemp("kg") / "graph.kuzu"
    with KuzuGraphStore(path) as s:
        load_into(s, extraction)
    return path


def _neo4j_available() -> bool:
    if not os.environ.get("NEO4J_PASSWORD"):
        return False
    try:
        from log_on.kg import Neo4jGraphStore
        Neo4jGraphStore().close()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="session", params=["kuzu", "neo4j"])
def graph(request, kuzu_path, extraction):
    """Read-only graph store; every QA test runs on both backends (Neo4j skipped if no server/credentials)."""
    if request.param == "kuzu":
        from log_on.kg import KuzuGraphStore
        s = KuzuGraphStore(kuzu_path, read_only=True)
    else:
        if not _neo4j_available():
            pytest.skip("Neo4j not reachable (set NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD to run)")
        from log_on.kg import Neo4jGraphStore, load_into
        with Neo4jGraphStore() as w:
            load_into(w, extraction)
        s = Neo4jGraphStore(read_only=True)
    yield s
    s.close()


@pytest.fixture
def p2(tmp_path, kuzu_path, monkeypatch):
    """Factory for Phase 2 orchestrators sharing one durable memory file (new instance = new 'session')."""
    from log_on.memory import MemoryManager, make_memory_store
    monkeypatch.setenv("LOG_ON_AS_OF", AS_OF.isoformat())
    mem_path = tmp_path / "memory.sqlite"

    def make():
        store, emb = make_memory_store(mem_path)
        return Orchestrator(planner=RuleBasedPlanner(), audit_path=tmp_path / "audit.jsonl",
                            outbox=tmp_path / "outbox.jsonl", memory=MemoryManager(store, emb), graph_path=kuzu_path)
    make.mem_path = mem_path
    make.tmp = tmp_path
    return make


def raw_bytes(path: Path) -> bytes:
    """All bytes of a database, including WAL/side files, for 'PII never stored' assertions."""
    out = b""
    for p in [path, *path.parent.glob(path.name + "*")]:
        if p.is_file():
            out += p.read_bytes()
        elif p.is_dir():
            out += b"".join(f.read_bytes() for f in p.rglob("*") if f.is_file())
    return out
