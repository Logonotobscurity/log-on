# DeepLearning.AI course link check — 2026-11-11

> **Caveat:** the learning-plan file (`be8b2c9c…txt`) was **not present on the box** when this phase was built
> (the attachments folder only contained an earlier PNG), so the 18 URLs *from the file* could not be checked one by one.
> Instead, the DeepLearning.AI courses that map to the LOG_ON layers and phases were checked with `curl -L`.
> Re-run `scripts/check_links.sh <file>` once the file is available. It extracts every URL from the file and checks it.

Method: `curl -s -L -o /dev/null -w '%{http_code}'`. A made-up slug on `www.deeplearning.ai` returns **500**, so a 200 there
is a real page. `learn.deeplearning.ai/courses/<anything>` returns **200 even for nonsense** (a client-rendered app), so it can't confirm a course exists.
Old `/short-courses/<slug>/` URLs now redirect to `/courses/<slug>`.

## Core for Phase 1 (all 200)
| Course | URL |
|---|---|
| AI Agents in LangGraph | https://www.deeplearning.ai/courses/ai-agents-in-langgraph |
| Long-Term Agentic Memory with LangGraph | https://www.deeplearning.ai/courses/long-term-agentic-memory-with-langgraph |
| Safe and Reliable AI via Guardrails | https://www.deeplearning.ai/courses/safe-and-reliable-ai-via-guardrails |
| MCP: Build Rich-Context AI Apps with Anthropic | https://www.deeplearning.ai/courses/mcp-build-rich-context-ai-apps-with-anthropic |

## Other layer-relevant courses that load (200)
agentic-ai · llms-as-operating-systems-agent-memory · evaluating-ai-agents · evaluating-and-improving-agentic-applications ·
red-teaming-llm-applications · llmops · automated-testing-llmops (CircleCI) · efficiently-serving-llms ·
governing-ai-agents (Databricks / Unity Catalog) · multi-ai-agent-systems-with-crewai ·
practical-multi-ai-agents-and-advanced-use-cases-with-crewai · ai-agentic-design-patterns-with-autogen ·
building-agentic-rag-with-llamaindex · improving-accuracy-of-llm-applications · acp-agent-communication-protocol ·
a2a-the-agent2agent-protocol · getting-structured-llm-output · reinforcement-learning-from-human-feedback ·
serverless-agentic-workflows-with-amazon-bedrock · building-ai-applications-with-haystack ·
claude-code-a-highly-agentic-coding-assistant · fast-and-efficient-llm-inference-with-vllm
(all under `https://www.deeplearning.ai/courses/<slug>`)

## Fail on www.deeplearning.ai (HTTP 500, i.e. no longer in the catalog)
| Old slug | Status | Where to go |
|---|---|---|
| quality-and-safety-for-llm-applications | 500 | Maybe still at `learn.deeplearning.ai/courses/<slug>` (can't be confirmed, see above). Nearest current course: `red-teaming-llm-applications`, `safe-and-reliable-ai-via-guardrails` |
| building-and-evaluating-advanced-rag | 500 | Current: `retrieval-augmented-generation` (course), `building-agentic-rag-with-llamaindex` |
| functions-tools-and-agents-with-langchain | 500 | Current: `ai-agents-in-langgraph` |
| evaluating-and-debugging-generative-ai | 500 | Current: `evaluating-ai-agents`, `evaluating-and-improving-agentic-applications` |
| fast-and-efficient-llm-serving-with-vllm (guess) | 500 | The real slug is **`fast-and-efficient-llm-inference-with-vllm`** (200; Red Hat, June 2026, hosted labs) |
