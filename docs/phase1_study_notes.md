# Phase 1 study notes: LangGraph · Guardrails · MCP → the LOG_ON code

How to use this: watch a lesson, open the file/function listed next to the concept, change something, then run
`pytest -q` and `python -m log_on.demo`. Line numbers are approximate. Search for the function name.

---

## 1. AI Agents in LangGraph (+ Long-Term Agentic Memory with LangGraph)
https://www.deeplearning.ai/courses/ai-agents-in-langgraph

| Concept taught | Where it lives in LOG_ON |
|---|---|
| Agent as a graph: **state, nodes, edges** instead of a hidden loop | `log_on/state.py::AgentState` (TypedDict state); nodes are `Orchestrator.intake/policy_check/planning/permission/approval/action/validation/finalize` in `log_on/graph.py` |
| `StateGraph`, `add_node`, `add_edge`, `START`/`END`, `compile()` | `graph.py::Orchestrator._build` |
| **Conditional edges** (router functions return the next node) | `graph.py::_after_policy`, `_after_planning`, `_after_permission`, `_after_approval` wired with `add_conditional_edges` |
| Partial state updates (nodes return only the keys they change) | every node returns a small `dict` delta, e.g. `policy_check` returns `{"policy": ...}` |
| **Persistence / checkpointer, threads** (`thread_id`) | `InMemorySaver()` in `Orchestrator.__init__`; `SqliteSaver` in `log_on/__main__.py`; `thread_id = run_id` in `start()` / `resume()` |
| **Human-in-the-loop**: pause before risky actions, inspect state, resume | `graph.py::approval` calls `interrupt(...)`; `resume()` sends `Command(resume={...})`; tests read `graph.get_state(cfg).next == ("approval",)` in `tests/test_graph.py::test_unapproved_action_halts_then_resumes_after_approval` |
| Tool-calling agent pattern (LLM decides → tool runs → observe) | split into `planning` (decide) → `action` (run tools via MCP) → `validation` (observe safely) |
| Memory (long-term course): semantic / episodic / procedural, `Store` | **not built yet**. Phase 2 tickets M-1…M-3 in `roadmap.md`. The audit log already holds episodic data you can mine. |

v1 note: the course uses `MemorySaver` and `interrupt_before=[...]`. Current LangGraph (1.2) prefers `InMemorySaver` and the
`interrupt()` function inside a node, which is what we use. It lets the node compute *what* to ask the human about.

**Exercises**
1. **Edit-then-approve:** let the approver change the plan, e.g. `Command(resume={"approved": True, "edits": {"recipient": ...}})`. Apply the edits in `approval` before routing to `action`, and add a test.
2. **Time travel:** use `graph.get_state_history(cfg)` to list checkpoints for a run, then re-run from the checkpoint *before* `planning` with a different planner. Add `python -m log_on --history <run_id>`.
3. **Retry loop:** if `validation` blocks output, route back to `planning` once with a hint ("avoid customer notes"), capped by a `retries` counter in `AgentState`. This is the first real cycle in the graph.

---

## 2. Safe and Reliable AI via Guardrails
https://www.deeplearning.ai/courses/safe-and-reliable-ai-via-guardrails

| Concept taught | Where it lives in LOG_ON |
|---|---|
| Failure modes of LLM apps: hallucination, PII leaks, off-topic, toxic or competitor mentions | input side: `log_on/policy.py::RULES`; output side: `log_on/guards/validators.py` |
| **Validator** = a function that returns pass/fail plus an optional fix | `guards/base.py::Validator.validate` → `PassResult` / `FailResult(error_message, fix_value)` |
| **on_fail policies**: fix, exception, refrain, noop (also reask and filter in Guardrails) | `guards/base.py::OnFail` and `Guard.validate` (FIX chains the fixed value, EXCEPTION blocks, REFRAIN empties, NOOP only logs) |
| **Guard** composes validators: `Guard().use(...)` | `guards/base.py::Guard` (native) and `guards/guardrails_backend.py::GuardrailsGuard` (real guardrails-ai `Guard().use(...)` with `@register_validator`) |
| PII detection (the course uses Presidio and Hub `DetectPII`) | `EmailRedactor`, `PhoneRedactor`, `NINRedactor`, `CardRedactor` (Luhn check, card-shaped grouping) |
| Keeping the chatbot on topic and blocking topics | `BlockedTopics` (wordlist; swap in a zero-shot classifier as an exercise) |
| Checking for hallucination and groundedness | **not built.** See the exercise below and roadmap V-2 |
| Running guards on both input and output | input: `policy` node; output: `validation` node; egress: `mcp_client.py::_acall` scrubs args for `open_world_hint` tools; audit: `audit.py::AuditLog._scrub` |

Why two backends: Hub validators like `DetectPII` and `ToxicLanguage` need `guardrails hub install` and a token. Our validators register locally, so
the same code runs offline, and you can compare the backends (`tests/test_units.py::test_guardrails_backend_matches_native`).
Note that guardrails-ai sends OpenTelemetry data to its hosted endpoint by default. We switch that off with `Guard.configure(allow_metrics_collection=False)`
and `OTEL_SDK_DISABLED`.

**Exercises**
1. **Presidio validator:** `pip install presidio-analyzer` plus a spaCy model, then write `PresidioPII(Validator)` that catches names and addresses (regex can't). Add it to `default_validators()` and check that `"Adaeze Okafor"` gets redacted.
2. **Groundedness check:** for `get_report`, validate that every number in the final answer appears in the tool output (`metadata["sources"]`). Fail with `OnFail.EXCEPTION`. This is the cheap version of the course's provenance validator.
3. **Measure your guards:** build a 50-row labelled CSV (text, has_pii, should_block) under `tests/data/` and a script that prints precision and recall per validator. Use it to tune the NIN rule, which currently has false positives on any 11-digit number.

---

## 3. MCP: Build Rich-Context AI Apps with Anthropic
https://www.deeplearning.ai/courses/mcp-build-rich-context-ai-apps-with-anthropic

| Concept taught | Where it lives in LOG_ON |
|---|---|
| Why MCP: M×N integrations become M+N, and tools are portable across hosts | the `action` node only knows "call tool X on an MCP server". Swap the server and nothing else changes |
| Host / client / server roles | host = `Orchestrator` (`graph.py`); client = `mcp_client.py::_acall` (`ClientSession`); server = `mcp_server/business_server.py` |
| **Tools** defined with decorators (`@mcp.tool()` in FastMCP v1, `@server.tool()` with `MCPServer` in v2) | `business_server.py::lookup_customer`, `get_report`, `send_report`, `calculator` |
| **stdio transport**: the client spawns the server as a subprocess | `mcp_client.py::server_params` (`StdioServerParameters`) and `stdio_client(...)` |
| Session lifecycle: `initialize` → `list_tools` → `call_tool` | `mcp_client.py::_acall` |
| Tool discovery and refusing unknown tools | `_acall` returns "tool not offered by server" if the name isn't listed |
| Tool annotations (`readOnlyHint`, `destructiveHint`, `openWorldHint`) | set in `business_server.py`; read in `mcp_client.py` (egress scrub) and cross-checked in `permissions.py::annotation_mismatches`. Per the spec they are *hints*, so the authoritative risk tiers stay client-side in `TOOL_POLICY` |
| Resources and prompts primitives; remote (Streamable HTTP) servers; connecting to Claude Desktop | **not built.** See the exercises below |

Version note: the course was recorded on **mcp v1** (`from mcp.server.fastmcp import FastMCP`). This project uses **mcp 2.3.0**, where FastMCP was
renamed `MCPServer` (`from mcp.server.mcpserver import MCPServer`). Importing the old path raises an error that links to the migration guide.

**Exercises**
1. **Add a resource:** expose `crm://customers/{id}` as an MCP *resource* (read-only context) and have the planner prefer resources over tools for reads. Then ask why that's safer.
2. **Go remote:** run the same server with `server.run("streamable-http")`, point the client at it with `mcp.client.streamable_http`, and add a bearer-token check. That's the start of secrets and auth (roadmap S-1).
3. **Connect a second server:** add a `files` MCP server (or the reference filesystem server) and route tools by server name in `TOOL_POLICY`, e.g. `{"files.read": low, "files.write": high}`. Test that a tool from an unknown server is denied.
