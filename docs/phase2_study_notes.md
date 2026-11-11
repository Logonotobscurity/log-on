# Phase 2 study notes: The Grounded Memory Subsystem

These notes cover the three Phase 2 courses. For each one: what it teaches (as I understand its published outline), where that idea lives in this repo, how the code differs from the course and why, and three exercises that build on the code.

| Course | Main idea | Where it is in LOG_ON |
|---|---|---|
| [Agentic Knowledge Graph Construction](https://www.deeplearning.ai/courses/agentic-knowledge-graph-construction) (Neo4j) | Turn structured and unstructured data into a graph through a schema-first, multi-step pipeline, then answer multi-hop questions with it | `log_on/kg/` |
| [Long-Term Agentic Memory with LangGraph](https://www.deeplearning.ai/courses/long-term-agentic-memory-with-langgraph) (LangChain) | Semantic, episodic and procedural memory in a LangGraph `Store`, namespaced per user | `log_on/memory/`, `graph.py` memory nodes |
| [Document AI: From OCR to Agentic Doc Extraction](https://www.deeplearning.ai/courses/document-ai-from-ocr-to-agentic-doc-extraction) (LandingAI) | Layout-aware parsing (text, tables, reading order) into structured JSON that is *grounded* (every value points back to where it came from) | `log_on/docai/` |

The run of the whole flow is in `docs/demo_output_phase2.txt` (`python -m log_on.demo --phase2`).

---

## 1. Agentic Knowledge Graph Construction

### Concepts mapped to code
| Course concept | In this repo |
|---|---|
| **User intent, then a schema proposal**: decide which questions the graph must answer, then design nodes and relationships for them, with a critic reviewing the proposal | `kg/schema.py` is a fixed, reviewed schema (6 node types, 9 relationship types). It was designed backwards from the target questions ("which suppliers serve customers whose contracts expire in Q1?"). `schema.describe()` is the text an LLM extractor sees. |
| **Domain graph from structured data** (CSV/tables mapped to nodes and edges) | `RuleBasedExtractor._from_table()`. Each table is recognised by its header signature (customer register, contract register, supplier register, allocations, contacts) and mapped to typed nodes and relationships. |
| **Lexical/subject graph from unstructured text** (chunk, then extract entities and relations) | `_from_text()`: splits text into sentences, finds known entity mentions (`Resolver.mentions`), detects unknown organisations (`ORG_RE`), and applies cue phrases ("is a subsidiary of", "subcontracts … to", "under contract", "led by"). `LLMExtractor` is the LLM version of this step. |
| **Entity resolution**: connect mentions in text to the same nodes as the structured data | `Resolver`: canonical keys (`Label:id`), aliases that ignore legal suffixes ("Atlantic Reefers Nigeria Ltd", "atlantic reefers"), and ambiguous aliases are dropped. The narrative line "Harmattan Haulage Ltd also provides … under contract LF-C-2403" attaches to the same `SUP-01` and `LF-C-2403` nodes as the tables. |
| **Critic/validation agents** | `Resolver.relate()` rejects relationships the schema does not allow, and `upsert()` reports conflicting property values. The narrative-vs-register expiry check flags a sentence whose date contradicts the contract table (`test_schema_validation_and_conflict_detection`). |
| **Inference / enrichment** | `_derive()`: `Supplier FULFILS Contract <- HAS_CONTRACT Customer` gives `Supplier SERVES Customer` (marked `derived=true`). |
| **Graph construction in Neo4j with Cypher `MERGE`** | `GraphStore.upsert_node/upsert_rel` use `MERGE … SET`, so re-ingestion is idempotent. The same code runs on `KuzuGraphStore` and `Neo4jGraphStore`. |
| **GraphRAG retrieval**: multi-hop Cypher instead of vector similarity | `kg/qa.py`: vetted, parameterised Cypher templates (2–3 hops), entity linking, fuzzy lookup, shortest path. Each answer carries the Cypher it ran, its hop count and page/section provenance. Exposed as the MCP tools `graph_query`, `find_path` and `entity_lookup`. |

**Differences from the course and why:**
- **The default backend is Kuzu (embedded, speaks Cypher), with Neo4j as the second backend.** Both run here and every QA test runs on both (`graph` fixture). Kuzu needs no server, so tests, CI and the MCP subprocess work without Docker or Java. Neo4j is what you would use in production for multi-writer access, Bloom/Browser and APOC. Risk: Kuzu upstream was archived in Oct 2025. 0.11.3 still works, and LadybugDB and Vela's fork continue it. The `GraphStore` interface makes the switch a single class.
- **Text-to-Cypher is optional, not the default.** The course has an LLM write Cypher. Here the rule planner sends the *question* to `qa.answer()`, which picks a reviewed template. Raw Cypher is accepted only after `assert_read_only()`, and the database is opened read-only anyway. That trades flexibility for being deterministic and safe, which suits a system whose Phase 1 is all about safety.
- **The extractor is rule-based by default.** No API key is available here. The LLM path is schema-checked and falls back to rules chunk by chunk, but it has only been tested with a fake model.

### Exercises
1. **Add a node type end to end.** Add `Invoice {id, amount_ngn_m, due_date, status}` with `(Contract)-[:BILLED_AS]->(Invoice)`. Extend `make_sample_pdf.py` with an invoice table, map it in `_from_table`, and add a template "Which suppliers are on contracts with overdue invoices?" (4 hops). Write the test first, as `test_q1_suppliers_multi_hop` does.
2. **Text-to-Cypher with a safety net.** Implement `qa.text_to_cypher(question)` with the LLM, given `schema.describe()` and two example queries. Run it on the questions the templates cover and compare its rows with the templates' rows (that is your eval). Make sure `assert_read_only` blocks a prompt-injected `DETACH DELETE`.
3. **Entity resolution stress test.** Add a paragraph that mentions "Atlantic Reefers" and "Eko Pharma Distribution Limited", plus a new company with a very similar name ("Atlantic Reefer Services Ltd"). Measure precision and recall of `Resolver.mentions` and `qa.lookup`. Then decide when the fuzzy threshold (0.6) should ask a human instead of guessing, and route that through the Phase 1 approval interrupt.

---

## 2. Long-Term Agentic Memory with LangGraph

### Concepts mapped to code
| Course concept | In this repo |
|---|---|
| **Short-term vs long-term memory.** Short-term is the thread's checkpointed state. Long-term lives in a `Store` shared across threads. | Short-term: the Phase 1 `InMemorySaver`/`SqliteSaver` keyed by `thread_id` (= `run_id`). Long-term: `langgraph.store.sqlite.SqliteStore` (`memory/manager.py::make_memory_store`), shared by every run and every process that opens the same file. |
| **Namespaces per user** | `MemoryManager.ns(user, kind)` -> `("memories", user, "semantic" \| "episodic" \| "procedural")`. `test_cross_session_recall_resolves_my_customer` shows user `tunde` cannot see Rhonda's facts. |
| **Semantic memory** (facts and preferences, searched by embedding similarity) | `remember_fact()` and `search()`. The `index={"dims": 384, "embed": …, "fields": ["text"]}` config embeds with local fastembed `bge-small-en-v1.5` and stores vectors with sqlite-vec. |
| **Episodic memory** (what happened in past interactions, used as examples) | `add_episode()` is written by the `memory_update` node after every completed, denied or no-plan run. The "What did I ask before?" recall reads it back. |
| **Procedural memory** (instructions that change the agent's own behaviour, updated from feedback) | `set_instruction()`: "From now on keep answers brief" sets `answer_style=brief`, which the `validation` node applies. `record_denial()`: a denied approval becomes a lesson that shows up as a `memory_warnings` entry the next time the same recipient is proposed. |
| **Memory tools / "hot path" vs background writes** | Writes happen in the graph's hot path but **after** the output guard (the `memory_update` node), so only validated content is stored. Reads happen in `memory_retrieval` **after** the input policy, so a refused prompt never reaches memory. |
| **Using memory in planning** | `knowledge_retrieval` resolves "my focus customer" via semantic memory, but only for self-references and only from memories that share content words with the goal. The planner then passes `entity_ids=["CUS-005"]` to `graph_query`. |

**Differences from the course and why:**
- The course uses LangMem's `create_manage_memory_tool` / `create_search_memory_tool` and an LLM that decides what to remember. Here the memory intents are explicit and rule-based (`parse_memory_intent`: "remember …", "from now on …", "what did I ask …"). It is deterministic and testable, and it means nothing is memorised unless the user says so.
- **Privacy rule:** every string goes through the Phase 1 redactor *before* it is embedded or stored. Credentials are rejected outright (`MemoryWriteRejected`), not just redacted. Recipient lessons are keyed by `sha256(recipient)`, so the raw address is never stored. The tests check the raw SQLite bytes, WAL included, for the raw values.
- The course's procedural memory rewrites the system prompt with an LLM optimiser. Here procedural memory is a small set of typed keys the runtime understands, plus free-text notes passed to the LLM planner as context. Self-rewriting prompts are deliberately left for Phase 4, behind human review and evals (see roadmap L-2).

### Exercises
1. **Memory with an LLM curator.** When `LOG_ON_LLM_API_KEY` is set, let an LLM propose `{"kind": "fact"|"instruction"|"none", "text": …}` from *any* goal (not just "remember …"). Every proposal must still pass `MemoryManager.guard`. Measure how often it proposes storing something it shouldn't, using an eval set of 20 goals that contain PII.
2. **Episodic few-shot planning.** Pull the 2 most similar past *successful* episodes (`search(kind="episodic")`) and add them to the LLM planner prompt as examples. Compare plan accuracy with and without them on the Phase 3 golden set (roadmap E-1).
3. **Forgetting and retention (NDPR).** Add "Forget …" (delete the closest semantic match after the user confirms through the approval interrupt) and a TTL for episodic memories (`SqliteStore(ttl=…)`). Write a test proving a forgotten fact can't be recalled from another session.

---

## 3. Document AI: From OCR to Agentic Doc Extraction

### Concepts mapped to code
| Course concept | In this repo |
|---|---|
| **From OCR to layout**: text alone loses structure, so detect regions (titles, paragraphs, tables) and reading order | `docai/pdf_ingest.py::_pdfplumber_pages`: tables via `find_tables()`, text *outside* table regions (`outside_bbox`), headings by font (bold, ≥12pt, wrapped titles merged), paragraphs by vertical gap, and blocks sorted top-to-bottom. |
| **Table extraction into rows and columns** | Each table becomes `kind="table"` with `table={"columns", "rows"}`, plus a linearised `"col: value; …"` text for search. |
| **Visual grounding**: every extracted value links back to its page and region | Every `Chunk` has `page`, `section`, `bbox` and `chunk_id`. The provenance flows into graph `sources` on every node and edge, and into every answer's `Sources: p.2 3. Contract Register` line. |
| **Schema-based field extraction** | The contract register is parsed into typed fields (`value_ngn_m` float, ISO `start_date`/`expiry_date`), and narrative dates ("31 December 2026") are normalised by `_iso()`. |
| **Agentic extraction (extract, validate, correct)** | Quality flags on chunks (`ragged_rows`, `empty_cells`, `no_layout`), the schema critic, and the register-vs-narrative consistency check. |
| **Fallbacks for hard documents** | The pypdf text fallback (`prefer="pypdf"` or automatic) keeps page provenance even with no layout. |

**Differences from the course and why:** the course uses OCR and vision models plus LandingAI's ADE API for scanned and complex layouts. The sample PDF here is born-digital with ruled tables, so pdfplumber can read its real structure on CPU, offline and without an API key. Scanned PDFs, charts and borderless tables are **not** handled yet (see the limitations in the README).

### Exercises
1. **Scanned input.** Rasterise the sample PDF (`pdftoppm -r 150`), OCR it with Tesseract or PaddleOCR, and rebuild chunks with the same `Chunk` schema (bbox from the OCR boxes). Compare the graph you get with the born-digital graph (`Extraction.stats()` plus the QA answers in `test_phase2_kg.py`). That gives you an end-to-end OCR quality metric.
2. **Borderless tables.** Regenerate the contract table without grid lines (`GRID` off in `make_sample_pdf.py`). `find_tables()` will miss it. Add a fallback that uses `table_settings={"vertical_strategy": "text", "horizontal_strategy": "text"}`, then validate the result with the `ragged_rows` check before trusting it.
3. **Agentic field extraction with grounding.** Given a JSON schema (`{"contract_id", "customer", "expiry_date", "value_ngn_m"}`), have an LLM fill it from chunk text, and require it to return the `chunk_id` it used for each field. Reject any field whose value is not literally present in the cited chunk. That is the core idea behind "visual grounding" as a hallucination guard.
