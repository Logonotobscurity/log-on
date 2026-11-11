# LOG_ON roadmap: analysis of the plan, tickets for Phases 2–4, and what next

> Phase 1 was planned from the plan's outline. Phase 2 was built against the full plan text (18 courses across 6 pillars and
> 4 phases). The 13-layer order is intake → memory → policy → orchestration → planning → permission → action → validation → approval →
> execution → audit → evaluation → learning.

## Status
| Phase | Plan deliverable | Status |
|---|---|---|
| 1 Safe Orchestrator | Goal → MCP tool → blocked if output violates a guardrail | **Done** (26 tests) |
| 2 Grounded Memory | Raw PDF → entities in Neo4j → multi-hop QA from the orchestrator, with session state | **Done** (38 tests). Kuzu embedded is the default backend and Neo4j 5 is supported and tested on the same Cypher. Long-term memory has semantic, episodic and procedural stores |
| 3 Autonomous Loop & Telemetry | Verification loop, W&B traces, CI eval gate | **Done** (39 tests; 50-case eval suite, 1.0 with the rule planner). Verify → revise → retry → escalate loop, per-run budgets, OpenTelemetry traces (local JSONL; OTLP / Langfuse optional; W&B replaced by OTel), trajectory grader, GitHub Actions gate |
| 4 Scale & Optimization | vLLM, DSPy, Giskard red-team | **Done, with caveats** (156 tests incl. 53 Phase 4; eval gate 242 cases incl. 186 red-team regressions, 1.0). OpenAI-compatible serving layer, vLLM GPU config (not run: no GPU) + llama.cpp CPU path benchmarked (Qwen2.5-1.5B), DSPy BootstrapFewShot with grader metric and human-approval registry (3 versions proposed, all correctly refused by the gate), red-team harness (19 → 0 successful attacks; live model 3 → 0) + Giskard scan (0 issues) |

**Deviation from the plan's order:** the plan puts *memory* before *policy*. LOG_ON runs `memory_retrieval` **after** the input policy,
so a refused or injected prompt never reads (or poisons) memory or the graph. Memory *writes* happen after output validation.

## Strengths
- **Safety comes first in the order.** Policy, permission, validation and approval all sit before execution, and audit and evaluation come after. Most agent tutorials leave these out.
- Separating **action** (prepare) from **execution** (commit), with validation and approval in between, is the right pattern for actions that can't be undone. Phase 1 already shows why: `send_report` sent nothing until approved.
- Courses mapped to layers keeps the learning tied to building.
- Ending with an evaluation → learning loop shows the plan treats the agent as a system that improves over time, not a one-off demo.

## Gaps (missing from the plan)
| Gap | Why it matters | Ticket |
|---|---|---|
| **Cost and token tracking, budgets** | LLM and tool spend per run, per user and per day. Without it an agent loop can burn money | O-1 |
| **Secrets management** | API keys, MCP server credentials and DB passwords. These shouldn't sit in `.env` in a repo | S-1 |
| **Approval UI** | ~~Approval is currently a CLI flag.~~ **Done (post-Phase-4 step 1):** `python -m log_on.approval_ui`, a queue with a redacted preview of what will happen, named approvers, notes, expiry | A-1 |
| **Identity and authorisation (who is asking)** | Policy doesn't depend on the user yet. Needs RBAC (role- and attribute-based) per tool and per data scope | S-2 |
| **Observability and tracing** | The audit log covers compliance, not debugging. Needs traces with spans and latency (OpenTelemetry, Langfuse or LangSmith) | O-2 |
| **Evaluation datasets** | "Evaluation" needs golden tasks plus adversarial (red-team) sets, versioned, before any "learning" | E-1, E-2 |
| **Rollback and compensation** | After execution, how do you undo it? Needs a per-tool compensating action and idempotency keys | X-2 |
| **Rate limits and timeouts per tool** | Stops runaway loops and protects downstream APIs | X-3 |
| **Data retention and NDPR/GDPR** | The audit log is append-only, but PII law needs retention limits and deletion rules. Redact before writing, as we already do | S-3 |
| **Prompt and plan versioning** | You need to know which prompt or policy version produced each decision. **Done in post-Phase-4 step 3** (provenance stamp in every audit record) | O-3 |

## Risky assumptions
- **Unity Catalog needs Databricks.** The "Governing AI Agents" course is Databricks-specific. OSS Unity Catalog exists but has far less tooling. **Recommendation:** model governance locally first (a tool registry plus a data-classification table in SQLite or Postgres), and only adopt UC if you're actually on Databricks.
- **vLLM needs a GPU.** Serving needs a CUDA GPU (CPU builds are slow and only for testing). The DL.AI vLLM course runs on hosted labs, so it's fine for *learning*. **Recommendation:** for the project, use a hosted OpenAI-compatible endpoint (`LOG_ON_LLM_BASE_URL` already supports it) or Ollama on CPU for small models. Only rent a GPU once eval numbers justify it.
- **CircleCI vs GitHub Actions.** The DL.AI "Automated Testing for LLMOps" course uses CircleCI. The skills carry over (the eval-in-CI pattern), but if the code lives on GitHub, **GitHub Actions** avoids a second vendor and second secrets store. **Recommendation:** GitHub Actions, and translate the course's config.
- **Course versions drift.** The MCP course is on SDK v1 (FastMCP), but v2 is current. The LangGraph course uses `interrupt_before`, but `interrupt()` is current. Expect to translate. Phase 1 already documents the differences.
- **Some course URLs are out of date** (see `course_links.md`). Several older courses return 500 on the main site.
- **"Learning" layer = automatic self-modification?** Letting the agent change its own policies or prompts with no human review undermines the safety layers. Learning should *propose* changes, a human approves them, and evals gate them.
- **Regex guardrails are a baseline, not a guarantee.** Treat them as the first line and measure recall (Guardrails exercise 3 in the study notes).

## Concrete tickets

### Phase 2: Memory, identity and secrets
- ~~**M-1**~~ **Done.** LangGraph `SqliteStore` with `("memories", user, semantic|episodic|procedural)` namespaces. The `memory_retrieval` node sits after policy and before planning. `test_cross_session_recall_resolves_my_customer` proves a second session uses a fact stored in the first.
- ~~**M-2**~~ **Done.** The `memory_update` node writes a redacted episode per run (goal, status, tools, first 300 characters of the validated output; never raw tool output).
- ~~**M-3**~~ **Done.** Writes go through the Phase 1 redactor and credentials are rejected. Tests scan the raw SQLite and WAL bytes.
- ~~**K-1**~~ **Done.** PDF ingestion with provenance, schema-first KG extraction, Kuzu and Neo4j stores, multi-hop QA via MCP tools.
- **K-2** LLM extraction + text-to-Cypher evaluated against the template answers (study notes, KG exercise 2). Needs an API key.
- **K-3** Incremental ingestion: per-document upsert and delete (the graph is rebuilt per load today), a document registry, and re-ingestion when a file's hash changes.
- **K-4** OCR path for scanned PDFs and a fallback for borderless tables (Document AI exercises 1–2).
- **K-5** Full-text and vector index on graph nodes (Neo4j FTS / Kuzu FTS) instead of in-Python entity linking, for large graphs.
- **K-6** Decide the long-term embedded graph engine (Kuzu 0.11.3 is archived upstream; candidates are LadybugDB or Neo4j only).
- **M-4** Forget/retention: a user-confirmed "forget …", a TTL on episodic memory, and purging checkpoints that hold raw goals (NDPR).
- **M-5** Optional LLM memory curator (implicit memories), gated by the same write guard and a PII eval set.
- **S-1** Secrets: load keys from env or a secrets manager (Doppler, 1Password CLI or GCP Secret Manager) through one `secrets.py`. Never log them. Add a `SecretLeak` scan to the audit writer and CI.
- **S-2** RBAC: `user` → roles → allowed tools and data scopes in `permissions.py`. `lookup_customer` needs the `support` role. Write tests for each role.
- **S-3** Retention: audit rotation plus a `purge --before` that keeps the hash-chain checkpoints.
- ~~**A-1**~~ **Done (post-Phase-4 step 1)** with Starlette server-rendered HTML + JSON API (`log_on/approval_ui.py`, `log_on/approvals.py`); Slack approval is still open. Original ticket: Approval UI: a small FastAPI page or Slack interactive message that lists pending interrupts (`graph.get_state`), shows the plan diff, approves or denies via `Command(resume)`, and auto-denies after expiry.
- ~~**V-1**~~ **Done (post-Phase-4 step 2)** for PII and prompt injection: Presidio with Nigerian NIN/BVN/phone recognizers (`log_on/guards/presidio_pii.py`) and an ONNX DeBERTa prompt-injection classifier (`log_on/guards/injection_ml.py`), regex kept as the first layer and fallback; results in `docs/safety_ml_report.md`. A toxicity/topic classifier is still open. Original ticket: Replace regex PII with Presidio. Add a zero-shot toxicity or topic classifier, keeping the regex as a fallback.

### Phase 3: Execution, real tools and evaluation
- **X-1** Split `action` (dry-run, which returns a proposed side effect) from `execution` (commit), with validation and approval in between, matching the plan's full order.
- **X-2** Idempotency keys and compensating actions per tool (`send_report` → `recall_report`).
- **X-3** Per-tool timeouts, retries with backoff, rate limits and a circuit breaker in `mcp_client.py`.
- **X-4** Remote MCP over Streamable HTTP with OAuth, plus a second real server (Gmail or Drive in read-only mode first).
- ~~**E-1**~~ **Done.** `evals/cases.yaml` (50 cases) + trajectory grader, run with `python -m log_on.evals` and in CI. Scores tool selection, order, policy, approval, output and efficiency.
- **E-2** *Partly done.* 18 attack cases (injection, PII exfiltration, tool misuse) gate CI at attack pass rate 1.0 and 0 PII leaks. Still to do: generated jailbreak variants (Giskard, Phase 4) and cases written independently of the rules.
- **E-3** LLM planner live: run E-1 with a real model behind `LOG_ON_LLM_API_KEY` and compare it with the rule planner.
- ~~**O-1**~~ **Done (per run).** `usage` ledger in state, audit and spans; `Budget` + `budget_stop` node. Per-user and per-day budgets are still open (**O-1b**).
- ~~**O-2**~~ **Done.** One span per node and per MCP call, route events, JSONL + `python -m log_on.trace`; OTLP / Langfuse exporters via env.
- **LP-1** Loop follow-ups: graph-answer verifier, reviser that learns from human retries (procedural memory), offline sweep of `max_attempts` vs. score (Phase 3 study notes).
- **LP-2** Keep one MCP session per orchestrator (or Streamable HTTP) to remove the ~1 s spawn per action the traces show.
- **E-4** Two-tier CI: rule evals per commit (done), model-graded evals nightly / on release with an Actions secret; baseline diff per case.

### Phase 4: Learning, deployment and governance
- **L-1** A feedback capture step (approver notes, deny reasons) goes into a dataset.
- **L-2** Proposal-only learning: a job that suggests new policy rules or few-shot examples from failures, opens a PR, and requires E-1 and E-2 to pass in CI.
- ~~**D-1**~~ **Mostly done in Phase 3.** `.github/workflows/ci.yml`: tests, eval gate, a simple secret pattern scan, artifacts. Lint (ruff) is still to add.
- **D-2** Container plus deploy (Cloud Run, Fly.io or Vercel functions for the API, with a Postgres checkpointer). **Container + Postgres checkpointer done (post-Phase-4 step 3)**: `Dockerfile`, `docker-compose.yml`, `LOG_ON_CHECKPOINTER=postgres`. A hosted deploy (Cloud Run / Fly.io) and HTTPS in front of the approval UI are still open.
- **D-3** Model serving decision: a hosted API vs vLLM on a rented GPU, decided on E-3 results and cost from O-1.
- **G-1** Governance registry: tools, data classes, owners and policy versions (local table; UC only if on Databricks).
- ~~**O-3**~~ **Done (post-Phase-4 step 3)**: `log_on/provenance.py` stamps code SHA, `POLICY_VERSION` + a hash of the policy/permission/guard sources and safety settings, the planner prompt id and provider/model into every audit record (inside the hash chain); `python -m log_on.audit verify` checks chain + stamps. Original ticket: Version prompts, policies and validator configs, and stamp the versions into every audit record.


## What next (after Phase 4)

Phase 4 is built, but three results say where the real work is:

1. **A stronger model.** The 1.5B CPU model plans validly 88–94% of the time and its DSPy-optimised prompt failed
   the safety gate. Next: run `deploy/vllm` on a rented GPU (Qwen2.5-7B/14B) or set a hosted key, re-run
   `log_on.serving.bench`, `log_on.learning optimize` and `log_on.redteam --planner llm`, and record the numbers
   (ticket D-3 can then be decided on data).
2. **Promotion with real data (L-1, L-2).** Capture approver notes and deny reasons as training data, add safe-refusal
   examples, try `--optimizer mipro`. Promote only when the full-suite gate passes, with a named approver.
3. ~~**Beyond regex safety (V-1).**~~ (done: step 2 below) The policy and redactor are still pattern-based; add Presidio / an ML injection
   classifier behind the same interfaces and re-run the red team. Grow the attack seeds beyond 14.
4. ~~**Operational gaps.**~~ (done: steps 1 and 3 below) ~~An approval UI instead of the CLI~~, ~~container (D-2) with a Postgres
   checkpointer~~, ~~stamping prompt/policy versions into every audit record (O-3)~~, ~~per-user/day budgets~~. A hosted deploy is open.
5. **CI.** The GitHub Actions workflow (tests, eval gate, red-team gate, registry check) is in place but cannot run
   while the GitHub account has a billing lock. Clearing that makes every push gated automatically.

### Post-Phase-4 progress (approved order: 1 approval UI → 2 smarter safety checks → 3 production hardening)

| Step | Scope | Status |
|---|---|---|
| 1 Approval web UI (A-1, feeds L-1) | Local web app listing LangGraph approval interrupts with a redacted preview (steps, risk tiers, destinations, policy/permission reasons, memory warnings); named approvers via `LOG_ON_APPROVER_TOKENS`; approve / reject with a note (reason required to reject); expiry (`LOG_ON_APPROVAL_TTL_S`, default 24 h, then auto-deny); stale-form check; every decision in the run's hash-chained audit log and in the hash-chained feedback log `runs/feedback.jsonl` (learning data for L-1). CLI shares the same service (`--pending`, `--by`, `--note`) | **Done** (10 tests) |
| 2 Smarter safety checks (V-1) | Presidio (Nigerian NIN/BVN/phone, cards, IBAN/IP/crypto) and an ONNX prompt-injection classifier behind the existing policy/redactor/memory interfaces, regex layer first and as fallback (`LOG_ON_PII_BACKEND`, `LOG_ON_INJECTION_ML`); quarantine of injected sentences in tool output / retrieved documents; red team grown 186 → 532 attacks (Nigerian languages, encodings, hidden comments, multi-turn, poisoned documents), found 3 vulnerable + 125 contained bypasses, fixed to 0 with the rule and live Qwen2.5-1.5B planners; 532 regression cases in the eval gate; `python -m log_on.evals.safety_bench` (PII recall 85% → 100%, held-out injections 1/20 → 13/20, +23 ms p50) | **Done** (42 new tests; suite 166 → 215) |
| 3 Production hardening (D-2, O-3, budgets) | Multi-stage non-root `Dockerfile` (`WITH_ML`, `GIT_SHA` build args) + `docker-compose.yml` (approval UI, one-shot app, Postgres 17, optional llama.cpp profile; secrets only via `.env`, `.env.example` placeholders) + `.dockerignore`; `LOG_ON_CHECKPOINTER=sqlite\|postgres\|memory` (pooled `PostgresSaver`, tested against a real local Postgres 16); O-3 provenance stamp in every audit record + `python -m log_on.audit verify`; per-user/day runs/tokens/cost budgets enforced at intake and before LLM-backed steps, persisted in SQLite or Postgres, `quota_stop` audit record; `/readyz` on the approval UI; CI Postgres service + hadolint/compose checks. Docker itself was not available on the dev box: Dockerfile linted with hadolint 2.15 (clean), compose validated with `docker compose config`, wheels resolved for Python 3.13 | **Done** (19 new tests; suite 215 → 234) |
| Dashboard (`/ui`) | Deckle-styled server-rendered dashboard in the approval app: request journey through the 13 layers from the audit log, approval queue on the existing CSRF-protected handler, audit chain + provenance explorer, eval/red-team/safety-bench evidence with claim-vs-breaking-point panels, budgets/telemetry/serving/DSPy ops view, generated thesis/antithesis analysis; JSON per view; demo seeder; feedback log now provenance-stamped | **Done** (12 tests) |

| Hosted providers + LLM grading | Groq / OpenRouter presets (key isolation, User-Agent, 429 retry + pacing, per-provider prices), `log_on.serving.compare`; outcome + safety grading for LLM planners (thresholds unchanged, mutation-tested, rule planner 1.0 in both modes). Groq `gpt-oss-120b` passes the full gate (0.995), `qwen3.8-27b` 0.993, local 1.5B 0.988 (fails) | **Done**; default planner **not** switched (needs Rhonda's OK) |

All three approved post-Phase-4 steps are done. What needs Rhonda next:
0. **Planner default** (D-3, now decidable on data, `docs/provider_comparison.md`): offer Groq `openai/gpt-oss-120b`
   as the default LLM planner (rules as fallback) after (a) escalating instead of `no_plan` when a benign goal gets an
   empty plan (E11), (b) choosing free-tier limits vs a paid Groq tier, (c) refusing sends to untrusted/attacker
   domains in the permission layer instead of asking a human (approval-bait case found with the 1.5B planner).
1. **A stronger model** (ticket D-3): a rented GPU for `deploy/vllm` (Qwen2.5-7B/14B) or a hosted API key, then re-run
   the serving benchmark, DSPy optimisation and the live-model red team. The 1.5B CPU model is too weak for prompt optimisation.
2. **GitHub billing**: clear the lock at https://github.com/settings/billing so CI (tests with a Postgres service, eval gate,
   red-team gate, Dockerfile/compose checks) runs on every push; then `gh workflow run ci.yml`.
3. **A first real `docker compose build`** on a machine with Docker (the image has not been built yet), and a decision on
   hosting (Cloud Run / Fly.io / a VPS) with HTTPS in front of the approval UI.
