import pytest

from log_on.graph import Orchestrator
from log_on.planner import RuleBasedPlanner


@pytest.fixture
def orch(tmp_path):
    return Orchestrator(planner=RuleBasedPlanner(), audit_path=tmp_path / "audit.jsonl",
                        outbox=tmp_path / "outbox.jsonl")
