# LOG_ON: Phase 1 (Safe Orchestrator) + Phase 2 (Grounded Memory)

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

## Phase 2: The Grounded Memory Subsystem

A PDF becomes a knowledge graph the orchestrator can query over several hops, and the agent keeps
privacy-safe long-term memory across sessions.

```
samples/*.pdf ──docai──> chunks.json (page, section, bbox) ──kg.extract──> entities + relations ──> Kuzu | Neo4j
                                                                                                   ▲
intake → policy → memory_retrieval → knowledge_retrieval → planning → permission → [approval] → action (MCP: graph_query / find_path / entity_lookup)
            │          (Store search)    (entity linking)       │                                   → validation → memory_update → audit
         refused ───────────────────────────────────────────────┼──────────────────────────────────────────────────────────→ audit
                                                  memory intent ("remember…", "from now on…", "what did I ask…") → validation → memory_update
```

| Layer | Implementation | File |
|---|---|---|
| Intake (Document AI) | pdfplumber layout parsing: ruled tables to columns/rows, headings by font to `section`, paragraphs by vertical gap, reading order. Every chunk has `page`, `section`, `bbox`, `chunk_id`. Quality flags. pypdf fallback | `log_on/docai/pdf_ingest.py` |
| Sample data | Fictional *Lagoon Freight & Logistics Ltd* FY2026 portfolio review (3 pages: customers, contracts, suppliers, allocations, contacts with PII, narrative) plus a deterministic generator | `samples/`, `scripts/make_sample_pdf.py` |
| Extraction | Schema-first. Tables are mapped by header signature, prose by cue phrases over resolved mentions. Entity resolution (ignores legal suffixes, aliases), `SERVES` inference, schema critic, register-vs-narrative check. Optional `LLMExtractor` (OpenAI-compatible, schema-checked, falls back per chunk) | `log_on/kg/schema.py`, `log_on/kg/extract.py` |
| Graph store | `GraphStore` interface with two Cypher backends: **Kuzu** (embedded, default) and **Neo4j** (`LOG_ON_GRAPH_BACKEND=neo4j`). Idempotent `MERGE`. Query handles are read-only (Kuzu `read_only=True`, Neo4j READ routing, plus a write-clause screen) | `log_on/kg/store.py` |
| Multi-hop QA | Vetted parameterised Cypher templates (2–3 hops), entity linking, fuzzy lookup, shortest path. Each answer has Cypher, hop count and `Sources: p.N section` | `log_on/kg/qa.py` |
| MCP tools | `graph_query(question \| cypher, entity_ids)`, `find_path(source, target)`, `entity_lookup(name)` on the existing server (read-only annotations, client-side risk tiers) | `log_on/mcp_server/business_server.py` |
| Long-term memory | LangGraph `SqliteStore` (sqlite-vec index) or `InMemoryStore`, namespaces `("memories", user, semantic\|episodic\|procedural)`, local **fastembed `BAAI/bge-small-en-v1.5`** embeddings (CPU) with a hashing fallback | `log_on/memory/` |
| Memory safety | Every write goes through the Phase 1 PII redactor before it is embedded or stored. Credentials are rejected. Lessons about recipients are keyed by hash. Retrieval happens only after the input policy | `log_on/memory/manager.py` |
| Orchestrator wiring | New audited nodes `memory_retrieval`, `knowledge_retrieval` (before planning) and `memory_update` (after validation or denial). Policy, approval and audit unchanged. Phase 2 is opt-in, so without it the Phase 1 graph is identical | `log_on/graph.py` |

Questions it answers (see `docs/demo_output_phase2.txt`):
- *Which suppliers serve customers whose contracts expire in Q1?* Supplier→Contract←Customer + SERVES, filtered by quarter. Q1 means Q1 2027 relative to today.
- *Which account managers have customers with contracts expiring in Q1, and which suppliers are involved?* (3 hops)
- *How is Sahel Telecom Towers connected to Onne Port Services?* (shortest path)
- *What is the group exposure of Zuma Cement Plc and its subsidiaries?*, *Who supplies Eko Pharma?*, *Where are the suppliers for Delta Agro Exports located?*
- Memory: *Remember that my focus customer is Eko Pharma Distribution Ltd* and then, in a **new session**, *Which suppliers serve my focus customer?*. Also *From now on keep answers brief* and *What did I ask before?*

## Run it

```bash
cd log_on
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt   # or python -m venv + pip
.venv/bin/python -m pytest -v                    # 64 tests (26 Phase 1 + 38 Phase 2; 6 need Neo4j, else skipped)
.venv/bin/python -m log_on.demo --phase2         # Phase 2 walkthrough (ingest → graph QA → memory)
.venv/bin/python -m log_on.ingest samples/lagoon_freight_portfolio_fy2026.pdf   # builds runs/graph.kuzu + runs/chunks.json
.venv/bin/python -m log_on "Which suppliers serve customers whose contracts expire in Q1?"
.venv/bin/python -m log_on "Remember that my focus customer is Eko Pharma Distribution Ltd"
.venv/bin/python -m log_on "Which suppliers serve my focus customer?"      # separate process = new session
.venv/bin/python -m log_on.demo                  # Phase 1: 5 scenarios, printed walkthrough
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
| `LOG_ON_GRAPH_BACKEND` | `kuzu` (default) or `neo4j` |
| `LOG_ON_GRAPH_PATH` | Kuzu database file (default `runs/graph.kuzu`) |
| `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` | Neo4j connection (env only; forwarded to the MCP server process, never written to disk) |
| `LOG_ON_EMBEDDINGS` | `fastembed` (default; ~67 MB model cached in `~/.cache/fastembed`) or `hash` (offline, no download) |
| `LOG_ON_AS_OF` | pin "today" (ISO date) so quarter questions are reproducible |

Neo4j (optional): `docker run -p7474:7474 -p7687:7687 -e NEO4J_AUTH=neo4j/<password> neo4j:5`, or without Docker
download the Neo4j 5 community tarball plus a Java 21 JRE, run `bin/neo4j-admin dbms set-initial-password <password>`, then `bin/neo4j start`.
Then run with `LOG_ON_GRAPH_BACKEND=neo4j NEO4J_PASSWORD=<password>`. The test suite runs the QA tests on both backends
whenever `NEO4J_PASSWORD` is set and the server is reachable.

Outputs land in `runs/` (`audit.jsonl`, `outbox.jsonl` — the mock "sent" reports, `checkpoints.sqlite`).

## Docs
- `docs/phase2_study_notes.md`: concepts from the Knowledge Graph, Long-Term Memory and Document AI courses mapped to code, with 3 exercises each.
- `docs/demo_output_phase2.txt`, `docs/test_results_phase2.txt`: captured Phase 2 runs.
- `docs/phase1_study_notes.md` — what the LangGraph, Guardrails and MCP courses teach, where each concept is in this code, exercises.
- `docs/roadmap.md` — analysis of the full plan, gaps, risky assumptions, Phase 2–4 tickets.
- `docs/course_links.md` — DeepLearning.AI URL check.
- `docs/demo_output.txt`, `docs/test_results.txt`: captured Phase 1 runs.

## Known limitations (honest list)

Phase 2:
- **Extraction is tuned to the sample document's shape.** Tables are recognised by header keywords and prose relations by a few cue phrases. A differently worded report needs new mappings, or the LLM extractor, which has only been tested with a fake model (no API key here).
- **QA covers the question types it has templates for.** Anything else gets a clear "couldn't map" message rather than a guess. Text-to-Cypher via the LLM planner is possible (raw read-only Cypher is accepted) but untested against a real model.
- **Memory intents are explicit phrases** ("remember…", "from now on…", "what did I ask…"). Nothing is memorised implicitly. Resolving "my …" through memory uses word overlap with the top semantic hits, which is a heuristic.
- **Only born-digital PDFs with ruled tables have been tested.** There is no OCR for scanned pages, and borderless tables or charts are not extracted.
- **Entity linking and lookup load all node names into Python**, which is fine for hundreds or thousands of entities but not for millions (use a full-text index then).
- **Kuzu upstream was archived (Oct 2025).** 0.11.3 works and is pinned. Forks (LadybugDB, Vela) continue it, and Neo4j is the supported alternative behind the same interface.
- **The redactor is still regex-based** (Phase 1 limitation), so memory and graph PII protection inherit its false negatives. Person *names* are not treated as PII. The LangGraph checkpointer (`runs/checkpoints.sqlite` in the CLI) stores the raw goal for pause/resume. Phase 3 should encrypt or scrub it.

Phase 1:
- The rule-based planner only understands the demo intents (calculate, look up customer, fetch/send report). It is a stand-in for an LLM, not a general planner. The LLM planner path has not been run against a real model (there's no API key here). Only its fallback and parser are tested.
- Guardrails are regex and wordlist based, so expect false negatives (an unlabelled phone in an odd format, toxicity phrased politely) and some false positives (any 11-digit number is treated as a NIN). ML validators (Presidio) are still open as roadmap ticket V-1.
- The MCP server process is spawned once per `action` node, which is simple and isolated but adds about 0.5 s per run.
- `send_report` is a mock. It writes to `runs/outbox.jsonl` and sends no email.
- The approval step uses the CLI and code only; there's no approval UI yet.
