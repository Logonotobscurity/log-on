"""LOG_ON Phase 1: The Safe Orchestrator.

intake -> policy -> planning -> permission -> [approval interrupt] -> action (MCP) -> validation -> audit
"""
import os as _os

# Never let library telemetry phone home from this project (guardrails-ai ships OTEL export on by default).
_os.environ.setdefault("OTEL_SDK_DISABLED", "true")

__version__ = "0.1.0"
