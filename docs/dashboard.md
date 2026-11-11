# LOG_ON dashboard (`/ui`)

A server-rendered dashboard inside the approval web app. It tells two stories: the journey of one request through
the agent, and how far the safety evidence really reaches. Every number is loaded on each request from files in this
repo or the runs directory. A demo seeder exists for screenshots; pages built from it carry a **DEMO DATA** banner.

![Story page](ui/story.png)

## Pages

| Page | What it shows | Source |
|---|---|---|
| `/ui` Story | Runs on record, approvals waiting, the eval gate, the audit chain seal, how runs ended, one example run per outcome, and the first two "claim vs breaking point" panels | `runs/audit.jsonl`, `docs/eval_report_step3.txt`, approval service |
| `/ui/journey?run=<id>` Journey | The 13 reference layers for the selected run (passed / blocked / paused / escalated / not reached / not needed), plus the audit records in chain order with their hashes. Keys: `j`/`k` | `runs/audit.jsonl` |
| `/ui/approvals` Approvals | Pending interrupts with the redacted preview (steps, risk, masked destination, policy and permission reasons, memory warnings) and approve/reject forms. The forms post to the existing `/approvals/{run_id}/decide` handler with its CSRF value, so the dashboard has no second approval path. Recent decisions | `ApprovalService`, `runs/feedback.jsonl` |
| `/ui/audit` Audit | Hash-chain verification of `audit.jsonl` and `feedback.jsonl` (`verify_report`), provenance stamps in use (O-3), a filterable record table (`?limit=`, default 100) | runs directory |
| `/ui/safety` Safety | Eval gate (cases, metrics, growth across phases, case mix), red-team verdicts before/after for Phase 4 and step 2, which disguises worked before the ML layer, safety bench regex vs combined, held-out wordings still passing, and seven claim/breaking-point panels | `docs/eval_report_*.txt`, `docs/redteam/*.json`, `docs/safety_bench.json`, `docs/safety_ml_report.md` (partial live "before" run) |
| `/ui/ops` Ops | Per-user daily budget meters (runs, tokens, cost), span latencies p50/p95/max from traces, token/cost totals, the llama.cpp serving benchmark, and the DSPy prompt versions the gate refused with the reasons | quota store, `runs/traces.jsonl`, `docs/benchmarks/*.json`, `prompts/registry.json` |
| `/ui/analysis` Analysis | "The state of LOG_ON": five themes, each written as thesis / antithesis / pragmatic next move from the loaded numbers | all of the above |

JSON for every view: `/api/ui/overview`, `/api/ui/runs`, `/api/ui/journey/<run_id>`, `/api/ui/approvals`,
`/api/ui/audit`, `/api/ui/safety`, `/api/ui/ops`, `/api/ui/analysis` (Bearer token or the session cookie; 401 otherwise).

## The 13 layers vs LOG_ON's graph

The reference architecture lists intake → memory → policy → orchestration → planning → permission → action →
validation → approval → execution → audit → evaluation → learning. LOG_ON's LangGraph differs, and the Journey page
says so rather than hiding it:

| Layer | Graph node(s) | Note |
|---|---|---|
| intake | `intake` | |
| memory | `memory_retrieval`, `knowledge_retrieval` | Runs **after** policy, so refused requests never read memory |
| policy | `policy` | Rules + ML classifier |
| orchestration | `quota_stop`, `budget_stop` | Routing is implicit; it only has a node when it stops a run |
| planning | `planning`, `replan` | |
| permission | `permission` | |
| action | `action` | |
| validation | `validation` | |
| approval | `approval`, `approval_decision` | Runs **before** action, not after validation |
| execution | `action` | Not a separate mechanism: no sandboxed executor |
| audit | `audit` | Finaliser |
| evaluation | `verification`, `escalation` | |
| learning | `memory_update` (+ feedback log) | |

![Journey page](ui/journey.png)

## Claim vs breaking point

Each panel pairs a headline with what it does not prove and the point where it stops being true, computed from the
files: e.g. *532/532 red-team attacks defended* vs *the set is 52 seed prompts × 24 rewriting techniques, the rules were
fixed on those attacks, and 7/20 held-out wordings pass the input check*. The interpretive sentences are opinion and
are kept in `log_on/dashboard/analysis.py` so they can be argued with in review.

![Claims](ui/safety_claims.png)

## Design

Deckle design system: dark letterpress. Tokens `--bg-dark #121110` … `--accent #d94e1a`; Playfair Display (debossed
headings via text-shadow), DM Sans body (line-height 1.7), DM Mono labels and data. Fonts are self-hosted from
`log_on/dashboard/static/fonts` (SIL OFL 1.1, licences alongside) with system fallbacks; no CDN at runtime. Charts are
inline SVG drawn on the server. About 50 lines of vanilla JS add the audit filter, `j`/`k` navigation and a reject-note
check; every page works without it. Skip link, visible focus rings, ARIA labels on charts and meters, responsive grid.

## Security

- Same approver tokens as the approval UI; unauthenticated pages redirect to `/login?next=…`, JSON returns 401.
- `next` redirects are whitelisted to `/ui…` paths (no open redirect).
- CSP: `default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; font-src 'self'; img-src 'self' data:;
  connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'`, plus no-frame, no-store, nosniff.
- Run text shown is what the audit log stored (redacted at write time) and is passed through the redactor again.
  Tests check that fictional NIN, phone and email values never appear in any page or JSON.

## Run it

```bash
.venv/bin/python -m log_on.dashboard.seed --runs-dir runs_demo        # optional: fictional demo runs (git-ignored)
.venv/bin/python -m log_on.approval_ui --gen-token ada                # prints an export line; keep it out of files
export LOG_ON_APPROVER_TOKENS="ada:<token-from-above>"
.venv/bin/python -m log_on.approval_ui --runs-dir runs_demo           # or runs/ for your real runs
# open http://127.0.0.1:8765/ui and sign in
```

In the container image `docs/` is not copied; mount it and set `LOG_ON_DOCS_DIR` or the evidence panels show
"no data".

## Limitations

- Budget meters show today's usage only (no history chart); telemetry percentiles come from the local JSONL exporter.
- The Journey layer mapping is a fixed table; a new graph node needs adding to `LAYERS` in `log_on/dashboard/data.py`.
- Pages re-read and re-verify the logs on every request; fine for thousands of records, not for millions.
- The analysis text is templated: the numbers update, the arguments do not.
