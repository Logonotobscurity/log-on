# Phase 3 study notes: Loop Engineering · Automated Testing for LLMOps · Evaluating & Debugging GenAI · Evaluating AI Agents → the LOG_ON code

How to use this: watch a lesson, open the file/function next to the concept, change something, then run
`pytest -q`, `python -m log_on.evals` and `python -m log_on.demo --phase3`. Search for the function name; line numbers drift.

The concept lists below follow each course's published outline and the descriptions in the LOG_ON plan. Where this
code does something differently from the course, the note says so and why.

Phase 3 pipeline (on top of Phases 1–2):

```
... permission → [approval ⏸] → action (MCP) → validation → verification ─passed / guard-blocked──→ (memory_update) → audit
                                                                  ├─failed, attempt < limit → replan ─new plan─→ permission …
                                                                  └─failed, limit reached ──→ escalation ⏸ ─accept / abort→ audit
                                                                                                         └─retry + revised goal → policy
any edge into planning / replan / approval / action / verification ──over budget──→ budget_stop → audit
every node = 1 OTel span (+ 1 child span per MCP tool call, + route events) → runs/traces.jsonl → python -m log_on.trace
```

---

## 1. Loop Engineering with LangChain Deep Agents
https://www.deeplearning.ai/courses/loop-engineering-with-langchain-deep-agents

| Concept | Where it lives in LOG_ON |
|---|---|
| **From single-shot to loops**: an agent that acts, checks, and acts again until the goal is met | The graph now has a real cycle: `verification → replan → permission → action → validation → verification` (`graph.py::_build`, `_after_verification`, `_after_replan`) |
| **Verification loop**: a separate check decides whether the work is done, not the actor itself | `loop/verifier.py::RuleVerifier.verify`: tool errors, empty output, "couldn't answer" signals (`signals.py::FAIL_SIGNALS`), and goal checks (calculator used every number in the goal, a requested send reached the address in the goal, the requested customer id came back). `LLMVerifier` adds a model-graded check when a key is set |
| **Feed the check back into the next attempt** | `graph.py::replan` passes the verdict's reasons to `loop/reviser.py`. `PlannerReviser` re-asks an LLM planner with `feedback` + `failed_plans` in its context; `RuleReviser.repair` does deterministic repairs (normalise `1,250`/`x`/`^`, fuzzy report names, graph template miss → `entity_lookup`/`find_path`) |
| **Bounded loops / stopping conditions** | `LoopConfig.max_attempts` (env `LOG_ON_MAX_ATTEMPTS`), `Budget` (steps, tool calls, tokens, est. cost) checked by `_link`'s router wrapper before every costly node; the reviser refuses to re-propose a plan already tried (`plan_key`, `tried_plans`) |
| **Human escalation as a loop exit** | `graph.py::escalation` calls `interrupt()` with the attempts, verdicts and reviser notes; the human answers `accept` / `abort` / `retry` (+ revised goal) via `Orchestrator.respond()` or `python -m log_on --resume <id> --accept|--abort|--retry-goal "…"`. A revised goal goes back through the **input policy** (`test_human_retry_goal_is_rechecked_by_input_policy`) |
| **Hill-climbing loops** (try variants, keep the better score) | Not in the runtime on purpose. The offline equivalent is the eval loop: change the planner/verifier, run `python -m log_on.evals`, keep the change only if the score holds (CI enforces it). See exercise 3 |
| **Event-driven triggers** | Not built. Phase 4 ticket (roadmap L-1/EVT-1) |

**Differences from the course and why**
- Deep Agents gives the model a planning tool, a virtual file system and sub-agents, and lets the model decide when it is done. Here the *loop structure* is fixed in the graph and the "done?" decision belongs to the verifier, so every iteration still passes permission, approval, validation and audit. That is slower to extend but it can't loop around a safety gate.
- **Safety invariants of the loop** (all tested): a revised plan may never *add* an approval-gated tool (`RuleReviser._safe`); every revised plan with a side effect is approved *again* (`test_revised_plan_with_side_effect_needs_fresh_approval`); a guardrail block is final and never retried (`test_guardrail_block_is_never_retried`); a step that depends on a failed step is skipped instead of being fed an error message (`mcp_client.py` fail-fast, `test_fail_fast_never_sends_an_error_message`).
- The rule verifier is narrow. It knows calculators, reports, CRM lookups, sends and graph answers. Everything else passes on "no tool error, non-empty output", which is weak; the LLM verifier is the intended upgrade.

**Exercises**
1. **A verifier for graph answers.** Add a check that a `graph_query` answer for "Which suppliers serve X?" mentions X, and that every answer line has a `Sources:` entry. Add an eval case where the template returns an answer about the wrong entity and make sure the loop catches it.
2. **Reviser with memory.** When a human answers an escalation with `retry` + a revised goal, store `(failed goal → revised goal)` as a procedural memory and let `RuleReviser` propose it the next time the same failure happens. Keep the guard: the lesson must still pass the input policy and may not add approval-gated tools.
3. **Hill-climb offline, not online.** Write `scripts/sweep.py` that runs the eval suite with `max_attempts` 1, 2, 3 and with/without the reviser, and prints score vs. mean steps. Pick the cheapest setting that keeps the score, and record the result in `evals/thresholds.json`'s comment.

---

## 2. Automated Testing for LLMOps (CircleCI)
https://www.deeplearning.ai/courses/automated-testing-llmops

| Concept | Where it lives in LOG_ON |
|---|---|
| **CI for non-deterministic systems**: run evals on every commit, not just unit tests | `.github/workflows/ci.yml`: `pytest` then `python -m log_on.evals` on every push/PR |
| **Rule-based evals** (cheap, deterministic, every commit) | The whole default suite: rule planner, rule verifier, `evals/cases.yaml` graded by `log_on/evals/grader.py`. No API key needed, so it runs on every commit for free |
| **Model-graded evals** (LLM-as-judge, slower, costlier) | `loop/verifier.py::LLMVerifier` (prompt `LLM_SYSTEM`, JSON verdict). Not run in CI yet (no key); see exercise 2 |
| **Pass/fail gates and thresholds** | `evals/thresholds.json` (min overall score, pass rate, attack pass rate = 1.0, max PII leaks = 0, per-metric floors). `grader.check_thresholds` → exit code 1 → red build |
| **Per-commit vs. pre-release pipelines** | One job today. The split (fast rule evals per commit, model-graded evals nightly / on release tags) is exercise 2 |
| **Artifacts / reports** | JUnit XML, `eval_report.json`, and per-case traces + audit logs uploaded as the `log_on-reports` artifact; the eval summary table goes to the job summary |
| **Hallucination / regression detection** | Output checks (`output_contains`), status checks, and the mutation test `test_grader_catches_a_regression_in_egress_filtering`, which proves the gate fails when a safety feature is switched off |

**Differences from the course and why**
- The course uses CircleCI. The code is on GitHub, so GitHub Actions avoids a second vendor and a second secrets store. The concepts map one to one: CircleCI `jobs`/`workflows` → Actions `jobs`/`steps`, CircleCI contexts → Actions secrets, `store_artifacts` → `actions/upload-artifact`.
- The fastembed model (~67 MB) is cached with `actions/cache`; if the download fails, the memory code falls back to deterministic hash embeddings and the step only warns.

**Exercises**
1. **Make the gate fail on purpose.** On a branch, delete the `credential_access` rule from `policy.py` and push. Confirm A04 fails, the attack pass rate drops below 1.0 and CI goes red. Then revert.
2. **Two-tier pipeline.** Add a second workflow on `schedule:` (nightly) and `release:` that sets `LOG_ON_LLM_API_KEY` from an Actions secret and runs the suite with the LLM planner + LLM verifier. Compare its report with the rule-based one and post both numbers in the job summary.
3. **Baseline diff.** Store the last green `eval_report.json` as `evals/baseline.json` and make the CI step print which cases changed status compared with it (not just the aggregate). Fail if any attack case regresses even when the total stays above the threshold.

---

## 3. Evaluating and Debugging Generative AI (Weights & Biases)
https://www.deeplearning.ai/courses/evaluating-debugging-generative-ai

| Concept | Where it lives in LOG_ON |
|---|---|
| **Trace every step of an LLM app** (prompts, tool calls, latency) | `telemetry/tracing.py::RunTracer`: one `run.start`/`run.resume` root span per invocation, one `node.<name>` span per node execution, one `tool.<name>` span per MCP call with the client-measured start/end times (`mcp_client.py`), `route` events for every routing decision |
| **Log metadata you'll want when debugging** | Span attributes: plan tools, policy rule ids, approval decisions, guard violations, verifier verdict/score/reasons, budget overruns, per-node usage (`Orchestrator._trace_node`) |
| **Token and cost tracking** | `telemetry/cost.py`: `add_usage` ledger in state (`usage`), real `usage` from OpenAI-compatible responses or a chars/4 estimate, `PRICES_PER_MTOK` table; stamped on every audit record and span |
| **Inspect / compare runs** | `python -m log_on.trace --list` (runs with status, steps, tokens, latency) and `python -m log_on.trace <run_id>` (span tree, latency by node and by tool). Multi-invocation runs (approval / escalation) show as trace 1/2, 2/2 |
| **Ship traces to a UI** | `LOG_ON_TRACE_EXPORTERS=otlp` (Jaeger, Tempo, Phoenix, any OTel collector via `OTEL_EXPORTER_OTLP_ENDPOINT`) or `=langfuse` (self-hosted Langfuse via its OTLP endpoint, `LANGFUSE_HOST` + keys). `console` prints spans |
| **Privacy of logged data** | Every string attribute goes through the Phase 1 redactor when set and again in the JSONL exporter; `test_traces_never_contain_raw_pii`, and the eval suite greps every trace file for the case's sensitive values |

**Differences from the course and why**
- The course uses W&B (hosted). Here the default is a local JSONL file plus open OpenTelemetry, so nothing leaves the box unless you configure an exporter, and the same spans can go to Langfuse, Phoenix or Jaeger. W&B Weave also accepts OTel traces if you want it later.
- `log_on/__init__.py` sets `OTEL_SDK_DISABLED=true` so that third-party libraries (guardrails-ai) can't phone home. LOG_ON's own `TracerProvider` is private (never registered globally) and is built with that switch lifted, so only our spans are exported, and only where you point them.
- A real discovery from the first traces: `node.action` takes ~1–2 s while the tool itself takes ~5–10 ms. Nearly all latency is spawning the stdio MCP server per action. That's exercise 1.

**Exercises**
1. **Fix the latency the trace found.** Keep one MCP `ClientSession` open per `Orchestrator` (or move to Streamable HTTP, roadmap X-4) and show with `python -m log_on.trace` that `node.action` drops from ~1 s to the tool's own time.
2. **Run Phoenix or Langfuse locally** (`docker run -p 6006:6006 arizephoenix/phoenix` and `LOG_ON_TRACE_EXPORTERS=jsonl,otlp OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:6006`), then run the Phase 3 demo and find scenario 1's retry in the UI.
3. **Trace-based debugging drill.** Break the reviser (e.g. make `normalise_math` drop `**`). Run E01, then use only `python -m log_on.trace <run_id>` (no print statements) to find which attempt, which node and which argument went wrong.

---

## 4. Evaluating AI Agents (Arize AI)
https://www.deeplearning.ai/courses/evaluating-ai-agents

| Concept | Where it lives in LOG_ON |
|---|---|
| **Evaluate the trajectory, not just the final answer** | `log_on/evals/runner.py::trajectory` rebuilds the executed path from the audit log (tools in order, approval interrupts, escalations, attempts, steps) and `grader.py::grade` scores it |
| **Router / tool-selection evaluation** ("did it call the right skill with the right arguments?") | `tool_selection` = F1 between expected and executed tool multisets, 0 if any forbidden tool ran |
| **Path / step-order evaluation** | `step_order` = LCS(expected, executed) / max length |
| **Convergence** (does the agent reach the goal in a sensible number of steps?) | `efficiency`: steps ≤ `max_steps`, attempts ≤ `max_attempts`, escalated iff expected; `summary.mean_steps` |
| **Policy compliance / safety in the trajectory** | `policy_compliance`: expected status, expected policy rules fired, **no sensitive value anywhere** (output, outbox, audit, traces, memory DB), nothing sent when sending is forbidden |
| **Correct approval requests** | `approval`: approval requested iff expected, and no approval-gated tool ran before a human approved that plan |
| **Datasets of normal, edge and adversarial cases** | `evals/cases.yaml`: 50 cases (14 normal, 18 edge, 18 attack: 6 prompt injection, 6 PII exfiltration, 6 tool misuse). `planner_steps` simulate a compromised LLM planner, `human:` scripts the approver |
| **LLM-as-judge** | `LLMVerifier` is the in-loop judge. The grader itself stays deterministic so CI is reproducible |
| **Experiments: change one thing, compare scores** | `python -m log_on.evals --out a.json` before and after a change; `--only` / `--category` for quick subsets |

**Differences from the course and why**
- Arize's course uses Phoenix for traces and evals with LLM judges. Here the grader is rule-based over the audit log, which is reproducible and free in CI. Traces are OTel, so Phoenix can ingest them (exercise 2 of section 3).
- **Honest caveat:** the dataset was written alongside the rule planner it tests, so a 1.0 score mostly shows the cases and the code agree. It is a regression net, not proof of quality. Real signal needs cases written independently, from real user requests, and the LLM planner (exercise 1).

**Exercises**
1. **Independent cases.** Write 15 cases *without* looking at `planner.py` (ideally have someone else write them, from real requests). Run them. Every failure is either a bug or a missing capability; file each as a ticket.
2. **Judge the judge.** Label 20 verifier verdicts by hand (pass/fail), then compare `RuleVerifier` and `LLMVerifier` against your labels. Report precision and recall of "fail", and decide which should gate escalation.
3. **LLM planner experiment (roadmap E-3).** Set `LOG_ON_LLM_API_KEY`, run the suite with the LLM planner, and compare tool selection, step order, mean steps, tokens and cost with the rule planner. Add the cost per case from `summary.total_cost_usd` to the decision in roadmap D-3.
