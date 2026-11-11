# LOG_ON roadmap: analysis of the plan and tickets for Phases 2–4

> I couldn't read the plan file (it wasn't on the box when this was built). This analysis works from its outline: the 13-layer
> pipeline (intake → memory → policy → orchestration → planning → permission → action → validation → approval → execution →
> audit → evaluation → learning), 18 DeepLearning.AI courses and 4 build phases, which mention Unity Catalog, vLLM and CircleCI.
> Please re-check it against the full text.

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
| **Approval UI** | Approval is currently a CLI flag. Real approvers need a queue showing a diff of what will happen, plus expiry | A-1 |
| **Identity and authorisation (who is asking)** | Policy doesn't depend on the user yet. Needs RBAC (role- and attribute-based) per tool and per data scope | S-2 |
| **Observability and tracing** | The audit log covers compliance, not debugging. Needs traces with spans and latency (OpenTelemetry, Langfuse or LangSmith) | O-2 |
| **Evaluation datasets** | "Evaluation" needs golden tasks plus adversarial (red-team) sets, versioned, before any "learning" | E-1, E-2 |
| **Rollback and compensation** | After execution, how do you undo it? Needs a per-tool compensating action and idempotency keys | X-2 |
| **Rate limits and timeouts per tool** | Stops runaway loops and protects downstream APIs | X-3 |
| **Data retention and NDPR/GDPR** | The audit log is append-only, but PII law needs retention limits and deletion rules. Redact before writing, as we already do | S-3 |
| **Prompt and plan versioning** | You need to know which prompt or policy version produced each decision | O-3 |

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
- **M-1** Add a LangGraph `Store` (SQLite, later Postgres) with namespaces `(user, "facts")` and `(user, "episodes")`, plus a new `memory` node between intake and policy that loads relevant memories into state. Done when a test shows the second run uses a fact stored in the first.
- **M-2** Episodic memory from audit: after `audit`, write a redacted run summary to the store. Never store raw tool outputs.
- **M-3** Memory write policy: memory writes go through the same Guard (no PII or secrets). Test it.
- **S-1** Secrets: load keys from env or a secrets manager (Doppler, 1Password CLI or GCP Secret Manager) through one `secrets.py`. Never log them. Add a `SecretLeak` scan to the audit writer and CI.
- **S-2** RBAC: `user` → roles → allowed tools and data scopes in `permissions.py`. `lookup_customer` needs the `support` role. Write tests for each role.
- **S-3** Retention: audit rotation plus a `purge --before` that keeps the hash-chain checkpoints.
- **A-1** Approval UI: a small FastAPI page or Slack interactive message that lists pending interrupts (`graph.get_state`), shows the plan diff, approves or denies via `Command(resume)`, and auto-denies after expiry.
- **V-1** Replace regex PII with Presidio. Add a zero-shot toxicity or topic classifier, keeping the regex as a fallback.

### Phase 3: Execution, real tools and evaluation
- **X-1** Split `action` (dry-run, which returns a proposed side effect) from `execution` (commit), with validation and approval in between, matching the plan's full order.
- **X-2** Idempotency keys and compensating actions per tool (`send_report` → `recall_report`).
- **X-3** Per-tool timeouts, retries with backoff, rate limits and a circuit breaker in `mcp_client.py`.
- **X-4** Remote MCP over Streamable HTTP with OAuth, plus a second real server (Gmail or Drive in read-only mode first).
- **E-1** Golden eval set (≥50 tasks: goal → expected plan, expected status) run with `pytest -m eval`. Report plan accuracy and refusal accuracy.
- **E-2** Red-team set (DL.AI red-teaming course): injection, jailbreak, PII exfiltration. Must hold ≥95% refusal or block with 0 PII leaks.
- **E-3** LLM planner live: run E-1 with a real model behind `LOG_ON_LLM_API_KEY` and compare it with the rule planner.
- **O-1** Cost tracking: a token and cost ledger per run in state and audit, plus a budget guard node that refuses when a run would exceed ₦/$ X.
- **O-2** OpenTelemetry traces (one span per node and tool call). Export locally to Jaeger or Langfuse.

### Phase 4: Learning, deployment and governance
- **L-1** A feedback capture step (approver notes, deny reasons) goes into a dataset.
- **L-2** Proposal-only learning: a job that suggests new policy rules or few-shot examples from failures, opens a PR, and requires E-1 and E-2 to pass in CI.
- **D-1** CI on GitHub Actions: lint, unit tests, eval suite and secret scan on every PR (translated from the CircleCI course).
- **D-2** Container plus deploy (Cloud Run, Fly.io or Vercel functions for the API, with a Postgres checkpointer).
- **D-3** Model serving decision: a hosted API vs vLLM on a rented GPU, decided on E-3 results and cost from O-1.
- **G-1** Governance registry: tools, data classes, owners and policy versions (local table; UC only if on Databricks).
- **O-3** Version prompts, policies and validator configs, and stamp the versions into every audit record.
