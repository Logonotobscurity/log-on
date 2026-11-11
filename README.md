# LOG_ON — Phase 1: The Safe Orchestrator

A working, offline, fully tested agent pipeline that implements the first half of the LOG_ON
architecture with real libraries:

```
intake → policy (input guard) → planning → permission ─┬─(high risk)→ approval ⏸ interrupt ─┬→ action (MCP) → validation (output guard) → audit
                                                       └─(low/med)───────────────────────────┘
refused / no plan / denied ─────────────────────────────────────────────────────────────────────────────────────────→ audit
```

| Layer | Implementation | File |
|---|---|---|
| Orchestration | LangGraph `StateGraph`, conditional edges, `InMemorySaver` / `SqliteSaver` checkpointer | `log_on/graph.py` |
| Intake / policy | Rule-based input policy (prompt injection, bulk PII export, credential requests, harm, fraud) | `log_on/policy.py` |
| Planning | Pluggable `Planner` protocol: deterministic `RuleBasedPlanner` (default) or `LLMPlanner` (OpenAI-compatible, only if a key is set; schema-checked, falls back to rules) | `log_on/planner.py` |
| Permission | Client-side tool risk tiers (low / medium / high); MCP annotations cross-checked but never trusted | `log_on/permissions.py` |
| Approval | `langgraph.types.interrupt()` + checkpointer; resume with `Command(resume={...})`, even from another process (CLI) | `graph.py::approval`, `__main__.py` |
| Action | Real MCP server (official `mcp` SDK v2, `MCPServer`, stdio) with `lookup_customer`, `get_report`, `send_report`, `calculator`; client discovers tools, resolves `{{steps[N].output}}`, scrubs PII from data leaving via open-world tools | `log_on/mcp_server/business_server.py`, `log_on/mcp_client.py` |
| Validation | Guardrails-style `Validator` / `on_fail` interface: email, phone (NG + intl), NIN, Luhn-checked cards → FIX (redact); secrets, toxicity/blocked topics → EXCEPTION (block). Two backends: native and **guardrails-ai 0.11** (custom validators via `@register_validator`, no Hub token, telemetry off) | `log_on/guards/` |
| Audit | Append-only, sha256 hash-chained JSONL; payloads PII-scrubbed before writing; `verify()` detects tampering | `log_on/audit.py` |

## Run it

```bash
cd log_on
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt   # or python -m venv + pip
.venv/bin/python -m pytest -v                    # 26 tests
.venv/bin/python -m log_on.demo                  # 5 scenarios, printed walkthrough
LOG_ON_GUARD_BACKEND=guardrails .venv/bin/python -m log_on.demo   # same, via guardrails-ai

# CLI with durable (SQLite) pause/resume across processes
.venv/bin/python -m log_on "Send the Q3 revenue report to finance@acme.example"
.venv/bin/python -m log_on --resume <run_id> --approve      # or --deny
.venv/bin/python -m log_on "Calculate (3+4)*12"
```

Environment variables:

| Var | Effect |
|---|---|
| `LOG_ON_LLM_API_KEY` (or `OPENAI_API_KEY`) | switch planning to `LLMPlanner` |
| `LOG_ON_LLM_BASE_URL`, `LOG_ON_LLM_MODEL` | any OpenAI-compatible endpoint (OpenAI, Together, a local vLLM/Ollama server) |
| `LOG_ON_GUARD_BACKEND` | `native` (default), `guardrails`, or `auto` |

Outputs land in `runs/` (`audit.jsonl`, `outbox.jsonl` — the mock "sent" reports, `checkpoints.sqlite`).

## Docs
- `docs/phase1_study_notes.md` — what the LangGraph, Guardrails and MCP courses teach, where each concept is in this code, exercises.
- `docs/roadmap.md` — analysis of the full plan, gaps, risky assumptions, Phase 2–4 tickets.
- `docs/course_links.md` — DeepLearning.AI URL check.
- `docs/demo_output.txt`, `docs/test_results.txt` — captured runs.

## Known limitations (honest list)
- The rule-based planner only understands the demo intents (calculate, look up customer, fetch/send report). It is a stand-in for an LLM, not a general planner. The LLM planner path has not been run against a real model (there's no API key here). Only its fallback and parser are tested.
- Guardrails are regex and wordlist based, so expect false negatives (an unlabelled phone in an odd format, toxicity phrased politely) and some false positives (any 11-digit number is treated as a NIN). Phase 2 swaps in ML validators.
- The MCP server process is spawned once per `action` node, which is simple and isolated but adds about 0.5 s per run.
- `send_report` is a mock. It writes to `runs/outbox.jsonl` and sends no email.
- The approval step uses the CLI and code only; there's no approval UI yet.
