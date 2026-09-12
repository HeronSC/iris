# Iris Master Development Checklist

## How to read this document

This is a **capability checklist, not a design**. Each line names something Iris should be
able to do. *How* it gets done is settled in a phase document when that work starts.

Conventions used below:

- `[ ]` not started · `[~]` partially built · `[x]` built and in use
- `->` points at the code or document where the work already lives
- **Options:** lists candidates to evaluate. Nothing on an Options line is a decision.
- **Owner:** on a subsection means that subsection is the *only* place that concern is
	specified. Other sections reference it rather than restating it, so a rule gets changed
	in one place.
- **Open:** marks a question that has to be answered before the item can be scheduled.
	All of them are collected again in Appendix A.

Related documents:

- `PhaseDesign.md` — the brief for the memory and learning phase.
- `PhaseStatus.md` — what that phase delivered, what was decided, what is still open.
- `core/Phase 11.md`, `ui/UI Phase 1.md`, `ui/UI Phase 2.md` — earlier per-phase briefs.

---

## 0. Where Iris is today

Written down so the unchecked boxes below read as "not yet" rather than "nothing exists".

| Area | State | Where |
|---|---|---|
| Local model access | Official `ollama` package behind a messages/tools/usage seam; still one model, no router | `core/llm/ollama_client.py`, `core/llm/models.py` |
| Memory and knowledge layer | Built: facts, observations, outcomes, decisions, hypotheses | `core/knowledge/` |
| Learning loop | Built: evidence, appraisal, promotion gate, review queue | `core/knowledge/hypotheses.py`, `review.py`, `appraisal.py` |
| Conversation, session, topic state | Built | `core/conversation/` |
| Document indexing and search | Built for pdf/docx/xlsx/csv/text | `core/documents/` |
| Actions and tools | Native actions plus MCP tools, all declared in one registry and run through one executor | `core/actions/`, `core/tools/` |
| Capability/provider routing | Built for a fixed provider list (weather, news, stocks, time, lookup) | `core/assistant/general_knowledge_router.py` |
| Intent routing and planning | Built; tool choice is native Ollama tool-calling over the registry | `core/assistant/intent_router.py`, `action_planner.py` |
| Audit log | Memory changes and actions only, not general | `core/audit/logger.py` |
| Request tracing | Built, optional | `core/conversation/request_trace.py` |
| Desktop UI | PySide6 desktop app | `ui/main.py` |
| HTTP surface | FastAPI | `serve.py`, `core/server/` |
| Voice | Empty package, nothing built | `core/voice/` |
| Trading bot integration | Built: bot posts observations, calls `/assess`; Iris scores and compares | `core/server/app.py`, bot's `bot/api/iris/client.py` |
| Generic tool/plugin registry | Built 2026-09-10: `ToolDefinition` + `ToolRegistry`, MCP client, `/tools` command | `core/tools/` |

---

## 1. Core Principles

- [ ] Build one Iris platform, not separate systems per app.
	Architecture: Iris Core -> Memory and Decision System -> Model Router -> Tool and Plugin Layer -> Applications and Systems.
- [ ] Evaluate existing open-source frameworks, APIs, and automation systems before custom-building.
- [ ] Keep memory, tools, and personality independent from any single model.
- [ ] Prefer native APIs and official integrations over GUI automation whenever possible.
- [ ] Every capability reaches the user through the tool layer, never as a special case wired
	directly into the UI.
- [ ] Local-only AI. Iris does not depend on any cloud AI provider — decided 2026-09-10 as a
	principle, not a deferral. Every model runs on this machine (2.4). Personal data, documents, and
	camera footage never leave it (10). Third-party *services* that are not AI (a search API, Graph,
	UniFi) are judged case by case in their own sections.
- [ ] Read-only first, then controlled write, then autonomous action — per integration, not globally.
- [ ] Few moving parts. Iris core is in-process — a Python library and SQLite files, no memory or
	vector server. The deliberate exceptions, each approved on its own on 2026-09-10, are: Ollama
	(already required), a Windows service hosting Iris's own headless half (11), and two local
	containers — SearXNG for search (5.1) and Home Assistant for devices (9.2, when it starts).
	Anything beyond that list needs the same explicit yes.
- [ ] Degrade gracefully. A missing model, an offline NAS, or an unreachable API produces a clear
	limitation, not a crash or a confident guess.
- [ ] Everything Iris knows or does is inspectable: why it answered that, where the fact came
	from, what it changed.

---

## 2. Foundation Capabilities

Sections 2.1–2.4 are the original core. Sections 2.5–2.8 were pulled up out of the feature
sections because more than one feature depends on each of them; leaving them buried inside a
single feature is what made those features look larger than they are.

### 2.1 Memory

**Owner:** everything memory-related. Other sections say "store it in memory (2.1)".

- [x] Support short-term conversation memory. -> `core/conversation/`
- [x] Support long-term persistent memory. -> `core/knowledge/repository.py`
- [x] Store personal preferences and working habits. -> `core/profile/`
- [x] Store project-specific knowledge. **Built 2026-09-12:** every record carries a `scope`
	(`global`, `project:<id>`, `session:<id>`); `/knowledge observe @project ...` and
	`@session` write into one, outcomes inherit their observation's scope, and `/knowledge
	scopes` counts them.
- [x] Retrieve the right memory at the right time. -> `core/knowledge/retrieval.py`
- [x] Record provenance on every memory: source, time, status.
- [x] Scope memory explicitly — global vs project vs session — and decide what a new project inherits.
	**Built 2026-09-12:** schema v3 adds `scope` (old databases are copied then migrated; every
	existing record is global). Recall filters to the scopes in view -- global, the active
	project (3.4), the active session -- and a record in the project's or session's own scope
	ranks a little above a global one on equal text, so the project's memory wins ties. A new
	project inherits only the global records; nothing is copied. The trading bot, corrections
	and the MCP server still write global records, which is right for them.
- [ ] Handle conflicting or superseded facts: which one wins, and whether the old one is kept.
- [ ] Decide a forgetting policy: expiry, decay, relevance pruning, or never forget.
- [ ] Let the user browse, correct, and delete memories directly, not only through conversation.
- [x] Export and back up memory in a readable format (11). **Built 2026-09-12:** `/knowledge
	export [folder]` writes `Data\Exports\knowledge-<stamp>.md` -- every live record grouped by
	topic with its kind, status, date, source, content and id, superseded records left out -- and a
	`.json` beside it with the full records. The backup (11) keeps the database; this is the copy
	a person can read.
- [x] **Decided and built 2026-09-10:** add embedding retrieval beside FTS5, not instead of it.
	-> `core/knowledge/embeddings.py`; fused in `retrieval.py`; `/knowledge embeddings` shows status.
	Embeddings from `nomic-embed-text` via Ollama; vectors stored beside `knowledge.db` (`usearch`
	since 2026-09-11, see the load-test line; `sqlite-vec` before that),
	so they share the store's transactions and backup. FTS5 and vector KNN are fused, then the
	existing scorer (recency, prior, diagnostics) applies unchanged. `core/knowledge/` otherwise stays.
	Considered and passed over: LanceDB (pyarrow), Chroma (onnxruntime, separate directory),
	FAISS/hnswlib (separate index files to keep in sync), Mem0/Letta/Zep (server-shaped, and they
	solve conversational recall, which is not the slice that needs help).
- [x] Embed everything except machine-generated records — those are near-identical prose and are
	already matched by numeric features in the appraisal. Built 2026-09-10 as a source-prefix rule
	(`bot:`, `iris:shadow`, `baseline:`; `knowledge.embeddings.skip_sources` in config). Measured on
	the real store: embedding the templated evaluator/shadow decisions too let them crowd out the
	human-written records in every nearest-neighbour search, so the rule covers all kinds, not
	only observations. 827 of 1,715 records embed, in about 5 s.
- [x] Load-test retrieval at ~400k records (a year of bot volume) before trusting it at scale.
	Run 2026-09-10 with random 768-d vectors: KNN is linear, ~2.3 ms per 1,000 rows (100k rows
	= 220-250 ms; 400k would be ~0.9 s). Two findings that shaped the code: (1) machine-generated
	records are excluded from the index, so the practical volume is human-written records — 827
	today, growing by hundreds a year, not hundreds of thousands; (2) sqlite-vec 0.1.9 segfaults
	intermittently on insert on this machine (about one crash per 50k-100k rows, any batch size),
	so vector writes ran in a child interpreter for a day. Replaced 2026-09-11 by `usearch`
	(single-file HNSW index beside the database, `core/storage/vector_table.py`), which removed
	the crash-isolation machinery; keys stay in step with SQLite the same way as before.
	sqlite-vec is brute-force KNN with no ANN index; this is where that either holds or does not.
- [x] Evaluate memory/orchestration frameworks before extending custom code:
	Mem0, Letta, Zep, Graphiti, Semantic Kernel, LangGraph, AutoGen, LlamaIndex, Haystack.
	(2.2 references this list rather than repeating it.)
	**Evaluated 2026-09-11, each against local-only, in-process, few moving parts:** Mem0 (generic
	conversational memory, PostHog on by default; not better than ours), Letta (a Postgres server;
	no), Zep (cloud only since April 2025; no), Graphiti (temporal graph, shaky on small local
	models; revisit), Semantic Kernel and AutoGen (retired into Microsoft Agent Framework), Agent
	Framework (viable, Ollama connector still beta; runner-up), LangGraph (adopted for the loop,
	see 2.3), LlamaIndex and Haystack (retrieval frameworks; ours is built), plus Cognee and txtai
	(same verdicts). Adopted the same day: LangGraph for the decision loop, usearch for vectors.

### 2.2 Decision-Making and Learning

- [x] Build the rational decision and learning layer. -> `core/knowledge/`
- [x] Store decision records with:
	situation and context, options considered, decision made, reasoning, outcome.
- [x] Never auto-promote a hypothesis; promotion is a human gate. -> `HypothesisTracker.promote`
- [x] When Iris is corrected, capture why the correction was made. **Built 2026-09-12:**
	`/correct <why>` records an observation under `iris/corrections` with the request id, what was
	asked and what was answered, so the why sits beside the what. `/corrections` lists them.
- [~] Turn repeated corrections into reusable principles. The third time the same correction is
	made, `/correct` says so and offers the line that turns it into a principle
	(`/knowledge observe iris/principles ...`); `/corrections` groups the repeats. Making that
	automatic, and feeding the principles into prompts, is still to do.
- [ ] Keep those principles in a list the user can read, edit, and switch off.
- [ ] Close the loop automatically where the outcome is observable without being told —
	a build failed, a file was reverted, an answer was rejected.
- [ ] Learn BC architecture and design preferences over time.
- [ ] Say what it does not know, and record the gap as an open question rather than guessing.
- [ ] Framework evaluation: use the single list in 2.1.

### 2.3 Plugin and Tool Architecture

- [x] Make plugin and tool architecture a core feature early. Built 2026-09-10: `core/tools/`
	holds the registry; `core/actions/implementations/` declare themselves into it.
- [x] Define a standard interface for adding tools. -> `core/tools/models.py` (`ToolDefinition`)
- [~] Let tools advertise their capabilities: name, purpose, typed arguments, cost, side effects.
	Name, purpose, typed arguments, permission, and confirmation are declared; cost is not yet.
- [x] Let Iris choose appropriate tools. -> native tool-calling in `intent_router._classify`
- [x] Declare each tool's permission level — read, write, execute. Enforcement lives in 10.
	Declared on every tool; MCP tools derive it from their annotations.
- [x] Support dry-run / preview for any tool that writes. **Built 2026-09-12:**
	`ActionExecutor.preview()` validates, asks the policy, and returns the target, the files that
	would be written, the diff and whether it would ask -- without running. Workflows dry-run
	through it.
- [ ] Give tools a uniform error and timeout contract, and make long-running tools cancellable.
- [x] Log every tool invocation and result (2.8). **Closed 2026-09-12:** the audit happens at
	the agent's dispatch point (`core/agent/graph.py` -> `core/tools/audit.py`), so command tools
	and knowledge providers are recorded the same way actions and MCP calls already were, and a
	tool nobody has written yet is covered by construction. Actions keep the executor's own entry
	rather than getting a second line: it knows the resolved target and the confirmation.
- [x] Version tools, so a saved workflow (8.3) does not silently change meaning. **Closed
	2026-09-12:** a saved workflow pins the version of every tool it uses and is blocked, with the
	change named, when one has moved; `/workflow rebase` accepts it. Bump `version` on a tool
	when its meaning changes, and the workflows that use it will say so.
- [x] Let a tool be disabled or sandboxed without removing it. -> `/tools disable <name>`
- [ ] Ensure future tools can be added without redesign — a new app integration (4) should be a
	plugin, not a core change.
- [x] **Decided and built 2026-09-10:** one tool definition — name, description, arguments as a pydantic
	model (JSON Schema for free), permission level, `requires_confirmation`. Native actions and the
	knowledge providers (`CapabilityDefinition`, already this shape) both become it. The registry is
	the only place a tool is declared; today adding one touches four.
- [x] **Decided and built 2026-09-10:** native Ollama tool-calling replaces the hand-written intent prompt in
	`_classify`. Verified on `qwen3:8b`: right tool, right arguments, no call for ordinary chat,
	under a second. Synonyms and learned examples stay as the fast path in front of it.
- [x] **Decided and built 2026-09-10:** adopt the official `mcp` Python SDK as a *client*. MCP servers register
	into the same registry and pass through the same validate → preview → confirm → audit spine in
	`core/actions/executor.py`, which stays. Node 22 and `uv` are installed for launching servers.
	Passed over on 2026-09-10: LangGraph, Semantic Kernel, pydantic-ai, smolagents — each wants to
	own the loop. **Reversed 2026-09-11:** letting a framework own the loop is exactly the point, and
	the hand-written loop is where the misroutes came from. LangGraph now owns it (`core/agent/`):
	classify → plan (one model call with every registry and capability tool bound) → act (through
	the executor; a confirmation is a LangGraph interrupt, resumed by the user's yes or no) → answer.
	Session state is checkpointed to `Data\Sessionsgent_checkpoints.db`, so a pending confirmation
	survives a restart. Iris keeps the model seam, tools, memory, and prompts; `orchestrator.py` is
	gone and the intent router keeps only its deterministic fast paths.
- [x] Later, once the client side works: expose Iris as an MCP *server*, so Claude Code and VS Code
	can call recall/observe/hypothesize directly. Cheapest route to 4.2. **Built 2026-09-12:**
	`mcp_server.py` over `core/mcp_server/`, stdio, on the official SDK's `MCPServer`. Eight tools:
	recall, observe, close_observation, hypothesize, add_evidence, assess, review_queue, explain.
	The process holds only the knowledge layer -- no watchers, no schedules, no window -- so an
	editor starting it does not start a second Iris. Every call passes the same permission policy
	(10) and lands in the same audit trail (2.8) as everything else, tagged with which client made
	it; a level the machine wants confirmed is refused rather than auto-approved, because there is
	nobody at the other end of an MCP call to ask. Accepting a hypothesis stays out of reach here
	too: a caller can bring evidence, and a person still decides.
	Point a client at it with `command: <venv>\python.exe`, `args: ["mcp_server.py", "--client",
	"claude-code"]`, `cwd: E:\AI\Iris`.

### 2.4 Model Router

**Decided 2026-09-10: Iris runs on local models only.** No cloud AI provider, now or as a
"later" item — it is a principle of the project (1), not a deferral. The router's job is to choose
among local models, not to decide when to leave the machine.

- [x] Do not permanently tie Iris to one model (for example, Qwen 8B). `config.json -> model` is
	now only the default; `models.tasks` routes per task class. -> `core/llm/router.py`
- [~] Support multiple specialized local models: chat, code, embedding, vision. Routes exist for
	code, embedding, and vision; no caller sends `code` or `vision` yet. Already pulled:
	`qwen3:8b`, `qwen2.5-coder:7b`, `qwen2.5vl:7b`, `mistral-small:24b`, `nomic-embed-text`.
- [~] Route on explicit signals: task class (the nine call sites are seven classes — chat, summary,
	follow-up, topic patch, intent, decision, proposal), required context size, latency budget,
	and VRAM (the RTX 4060 Ti has 16 GB; two large models do not fit at once). Task class is
	built 2026-09-10: every call site tags its task. Context size, latency, and VRAM are not.
- [x] Fall back cleanly when a model or host is down. A missing model follows `models.fallbacks`
	then the default; a down host raises one clear error and is recorded in metrics.
- [~] Require structured output / tool-calling support from any model used for planning.
	`/models` and startup warn when a planning model lacks the `tools` capability; not enforced.
- [x] Track tokens and latency per request, and make it visible (2.8). -> `core/llm/metrics.py`,
	`/models usage`, `/models recent`.
- [x] **Decided and built 2026-09-10:** replace the hand-rolled urllib client with the official `ollama`
	Python package — chat history, tools, streaming, and embeddings in one thin dependency.
	Today `stream: False` is hard-coded, which is why 6 cannot stream.
- [x] **Decided and built 2026-09-10:** a thin in-house router behind the existing `LLMClient` seam. The
	seam itself must grow from `(system, user) -> str` to a request/response that carries messages,
	tools, and usage — that is the real work here. -> `core/llm/models.py`, `core/llm/router.py`.
	Passed over: LiteLLM (normalises to OpenAI shape, heavy for a one-runtime problem), OpenRouter
	(hosted middleman), and the `anthropic` SDK (no cloud AI, by principle).
- [ ] Options for local runtimes beyond Ollama, if ever needed: llama.cpp, LM Studio, vLLM.
- [ ] Fixed 2026-09-10: `config.json` pointed at `localhost`, which resolves to `::1` first on
	Windows and stalled ~2 s per call before falling back. Now `127.0.0.1`; a chat call went from
	2,242 ms to 228 ms for identical work, and roughly five calls run per turn.

### 2.5 Conversation, Session, and Topic State

Already built and relied on everywhere, but missing from the original document.

- [x] Sessions with persistence and resume. -> `core/conversation/session_manager.py`
- [x] Summarise long conversations to stay inside the context budget. -> `session_summarizer.py`
- [x] Topic tracking. -> `core/conversation/persistent_memory/`
- [x] Build request context from the available sources. -> `context_builder.py`
- [ ] Branch or fork a conversation without losing the original.
- [ ] Search across past conversations.
- [ ] Attach a session to a project (3.4) so its context loads with that project.

### 2.6 Storage Abstraction

**Owner:** where files live. Moved here from 4.5 and 7.2, which each stated half of it.

**Decided 2026-09-10: the operating system is the storage abstraction.** `pathlib` over local
disk, UNC paths, and a OneDrive sync folder covers all three; `fsspec` was passed over as a layer
for a problem Windows already solves.

- [ ] NAS is reached by UNC path. Handle an offline or unreachable root without hanging or losing
	data — the scanner's `exists()` guard is the start; a UNC root that vanishes mid-scan is the
	case to test.
- [ ] OneDrive is not synced to this PC today. When it is needed, sync it rather than reaching it
	through Microsoft Graph, and turn Files On-Demand *off* for any indexed folder — a placeholder
	reads like a file but downloads on access, so indexing an on-demand tree pulls down the whole
	tree. Detect the placeholder attribute via `os.stat` and refuse to index it. Graph stays a 4.5
	question for mail and calendar, not storage.
- [x] Know which roots are indexed, and keep the index current as files move. -> `/index status`,
	`/index watch`; `core/documents/watch.py` applies creates, edits, moves, and deletes as they
	happen. Built 2026-09-11.
- [x] **Decided and built 2026-09-11:** `watchdog` for change detection — native Windows change
	notifications, one dependency serving this item and 8.2's file watchers. Scheduled rescans stay
	as the safety net, because a watcher can miss events during downtime: a full rescan every 24 h
	(`document_search.watch.rescan_interval_hours`), events coalesced for 2 s before applying.
- [ ] Respect path allowlists and per-root permissions (10).

### 2.7 Result Contract

**Owner:** the shape of an answer. Moved out of 5.1 and 6, which both described it.

- [~] Tools and providers return structured results — typed data plus display hints — never
	pre-formatted prose. **Contract built 2026-09-12** (`core/results/`); two producers speak it so
	far -- `fetch_web_page` (a link with its byline, then the text) and `disk_usage` (a table, a
	used/free chart, a warning when the scan was cut short) -- and every MCP tool's content arrives
	as results. `ActionResult.results` carries them beside the prose the model reads; the agent
	passes them through the graph and the window's detail panel renders them. The other actions
	and the five knowledge providers still return prose plus per-capability `facts`; each is a
	small change now that the shape exists.
- [x] Define the result types once: text, table, image, video, file, code, diff, link/card,
	chart, status. **Built 2026-09-12:** one `Result` with a `kind` and a validated payload rather
	than ten classes, so JSON round-trips are one function and a client that does not know a kind
	can still show its markdown. -> `core/results/models.py`.
- [x] Every result carries its source and timestamp, so it can be cited and re-run. `Source` is
	name, kind (tool, capability, mcp, web, document, memory) and a ref -- the URL, path or record
	id -- and `created_at` is set when the result is made, not when it is shown.
- [x] The UI (6) renders these types. Any client — desktop, web, voice — renders the same
	results in its own way. Every kind renders to markdown (`core/results/render.py`) for the
	console and the model, and since 2026-09-12 to HTML (`core/results/html.py`) for the desktop
	panel; the typed results ride in the detail's metadata and the window renders from those.
- [~] Results can be saved into memory (2.1) or attached to a project (3.4) as they are.
	`to_memory_record()` turns a result into an observation whose `data` keeps the whole result
	and whose source is the result's source, so a saved link is still a link. Attaching to a
	project waits on 3.4.
- [x] Map to and from MCP content blocks (text, image, resource), so an MCP tool's result (2.3)
	renders natively and an Iris result can be returned to an MCP caller later. **Built 2026-09-12:**
	`core/results/mcp.py` -- text becomes text (fenced text becomes code), image becomes image,
	an embedded resource becomes text, code, image or file by its MIME type, a resource link
	becomes a link, and structured content with nothing else becomes JSON code; the other way,
	images go out as image blocks and everything else as its markdown. In-house, no new
	dependency. `McpCallResult.results` is filled on every MCP call.

### 2.8 Observability and Audit

- [x] Log what Iris does. One stream, one schema since 2026-09-12: memory changes, actions,
	tool invocations (2.3), permission decisions (10), and scheduled jobs (8.2) all write
	`Data\Audit\audit.jsonl` through `core/audit/stream.py`. Model calls stay in `llm_requests`,
	which is a metrics table rather than an audit trail. Since 2026-09-10 every audit entry,
	metrics row, trace entry, and log line carries the turn's `request_id`.
- [x] Trace a request end to end: intent, plan, tools called, model used, timing.
	-> `core/conversation/request_trace.py` (now with request id, model, usage), the `turn` log
	line per request, `llm_requests`, and `actions.jsonl`, joined by `request_id`.
- [x] Answer "why did you do that?" from the trace, in the UI, without reading log files.
	-> `/why` (last request) or `/why <id>`: what was asked, how it was routed, the model calls
	with tokens and latency, the actions and their outcome, any warnings. Built 2026-09-10.
- [x] **Decided and built 2026-09-10:** `structlog` on top of stdlib `logging` — a request id bound once and
	present on every line, while `mcp`, `uvicorn`, and other libraries still log through stdlib and
	get captured. JSON lines, rotating file handler, size cap. -> `core/observability/`;
	`Data\logs\iris.jsonl`, 10 MB x 5; configured by the three entry points, never by tests.
- [~] **Decided 2026-09-10, table built:** metrics are a SQLite table of per-request rows — model, tokens,
	latency, tools called, outcome — queried by the UI. No metrics server. -> `core/llm/metrics.py`
	(`Data\Metrics\metrics.db`); read by `/models usage`, not yet by the UI. OpenTelemetry was
	passed over: the API package arrived with `mcp`, but a useful setup means an exporter and a
	Jaeger/Grafana backend, which is three services for one user on one machine.
- [x] **Built 2026-09-12:** one audit schema, one file. `AuditEvent` in `core/audit/stream.py`
	is deliberately small -- when, which request, what category, what happened, to what, how it
	ended -- with anything caller-specific in `data`. The two old loggers stay as adapters over it
	so their callers and tests are untouched, and `memory_changes.jsonl` and `actions.jsonl` are
	still read as history rather than migrated. `/why` now shows tool and permission lines beside
	the actions.
- [x] Never log secrets or credential values (10). **Built 2026-09-12:** redaction happens where
	a record is written, never where it is read -- a credential that reached the file is already
	leaked. A key whose name says credential is replaced whole; `user:password@host`, an
	`Authorization` header, and a token in a query string are rewritten in free text.
	-> `core/audit/redaction.py`.
- [ ] Retention policy for traces, audit entries, and captured screen or camera data.

---

## 3. Context and Developer Workflows

### 3.1 Active Context Awareness

**Owner:** "what is the user looking at right now." Sections 4.1 and 4.2 previously repeated
this; they now consume it.

- [~] Understand what the user is currently working on. **Built 2026-09-12:** `core/context/`
	samples the foreground window every 1.5 s in the desktop app, skips Iris's own window, and
	keeps the last twelve distinct contexts. What it knows is the app and, per provider, the file,
	workbook, folder, project and selection; what the user is *doing* with it is still inference.
- [x] Detect active application. Foreground window through `pywin32`, process name through `psutil`.
- [~] Detect active file, document, and project. VS Code from its title (file, workspace,
	unsaved marker); Excel through COM (`ActiveWorkbook.FullName`, active sheet); Word,
	PowerPoint and Acrobat from the title; File Explorer from the title when it is a path.
	VS Code gives a file name, not a path -- the path arrives with 4.2 (MCP) or a workspace
	search.
- [~] Detect the current selection where the app exposes one. Excel's selection address; nothing
	else yet.
- [x] Handle requests like:
	"Look at the workbook I have open."
	"Look at the project I have open in VS Code."
	"Look at this file."
	The system prompt carries "What the user is looking at" on every turn and the
	`active_context` tool returns it on demand with the file as a result, so the model passes the
	real path to the next tool.
- [x] Implement this generically: a context-provider interface with one small provider per app,
	not one-off handling per request. `ContextProvider` (`matches`, `capture`) in
	`core/context/providers.py`; the chain ends with a plain window provider so every app yields
	something.
- [~] Resolve "this" and "here" against the current context, and say which target was picked.
	`ContextService.resolve()` picks the current or the previous context and returns why; the
	model does the resolving from the prompt line today, and the tool's sentence names the target.
- [x] Keep a short history of recent context, so "the file I had open before" works. `/context
	history`, `active_context which=previous`, and "Before that:" in the prompt line.
- [x] Context capture is user-visible and can be paused (10). `/context` shows what Iris sees,
	`/context pause` and `/context resume` switch it, `context.enabled: false` in `config.json`
	starts it paused. While paused nothing is sampled and the prompt line is empty.
- [x] Options: Win32 foreground-window APIs, UI Automation, per-app adapters (Excel COM, a VS Code
	extension), window-title parsing as the crude fallback. **Chosen 2026-09-12:** Win32 foreground
	window plus per-app adapters, title parsing as the fallback; UI Automation waits on the
	`pywinauto` question in 8.1.

### 3.2 Coding and Development Assistant (Start with BC/AL)

- [~] **Routing fixed 2026-09-11** after a real session: a greeting was answered with the weather (the
	orchestrator picked a capability for small talk) and a request for a Business Central function became a
	file search for the word "to" (the deterministic parser treated any sentence containing lookup/list/show
	as a filename search). Now: small talk and code requests skip the orchestrator, a file search needs a
	file word, a filename, a path, or an imperative "find X"; code requests route to the `code` task
	(qwen2.5-coder) with guidance that Business Central means AL. -> `core/assistant/request_kinds.py`.

Git-specific items moved to 3.3.

- [x] Begin with read-only repository access. **Built 2026-09-12:** `core/code/` and three
	read-only tools (`core/actions/implementations/code_tools.py`), all confined to the document
	roots in `config.json` -- a path outside them is refused by name.
- [~] Provide repository search — text first, symbol-aware later. `repo_search` wraps `rg`
	(`core/code/search.py`): literal or regex, case, glob, capped, skipping `.git`, `.alpackages`
	and build output; results are a table of file, line, text. Symbol-aware search is the next
	line.
- [x] Provide symbol and object awareness. `al_symbol` looks an object up by name, id or prefix
	in the workspace's own `.al` sources and in the newest version of each `.alpackages` file:
	kind, id, namespace, caption, fields with types, procedures, the events it publishes, who in
	this workspace subscribes to them, and which extensions target it. Package symbols are parsed
	once and cached as compact JSON under `Data\Indexl_symbols` keyed by file size and time
	(the five packages of a BC 28 workspace take ~2 s cold, ~0.6 s from cache).
- [~] Build relevant context automatically before model calls. When VS Code is in front (3.1)
	with an AL workspace, the system prompt carries the app name, version, publisher, Business
	Central target and the open file on every turn. The objects the open file touches are not
	pulled in yet.
- [x] Understand relationships between AL objects: tables, pages, codeunits, extensions, events.
	Extensions to an object, events an object publishes, subscribers in the workspace (parsed
	from `[EventSubscriber(...)]`), interfaces an object implements.
- [x] Read `app.json`, `launch.json`, and `.alpackages` to know the app, its dependencies, and target.
	`al_workspace` (`core/code/workspace.py`): name, publisher, version, platform, application,
	runtime, id ranges, dependencies, AL launch targets, packages, and object counts by kind.
- [ ] Learn preferred BC patterns and architecture (feeds 2.2).
- [ ] Add controlled file editing later.
- [ ] Run the BC compiler and read errors.
- [ ] Correct its own changes.
- [ ] Run tests and read the results.
- [ ] Show code diffs before significant changes — rendered per 2.7, gated per 10.
- [ ] Target a VS Code-class coding experience.
- [x] **Decided 2026-09-10, built 2026-09-12:** symbol and object awareness comes from parsing `SymbolReference.json`
	inside the `.alpackages` files every AL project carries — in-house, no dependency. Verified on
	a BC 27.1 package set: 1,567 tables, 2,708 pages, 1,748 codeunits; `Customer` with 183 fields
	and 132 methods. Objects nest under `Namespaces` from BC 26 on, so walk recursively. The
	project's *own* objects come from its built `.app`, which carries the same file.
	Passed over: the AL language server (licensed as part of the VS Code extension, built to be
	driven by an editor), tree-sitter (no AL grammar), a custom AL parser.
- [~] **Decided 2026-09-10:** text search is `rg` wrapped as a native tool (ripgrep 15.1 is
	installed; built 2026-09-12); the compiler is `alc.exe` from the installed AL extension (18.0.2732683), wrapped
	the same way (not yet).

### 3.3 Git and Azure DevOps

- [x] Repository awareness (moved here from 3.2). **Verified 2026-09-12:** the git MCP server
	starts (the client resolves `uvx` from `~/.local/bin` itself) and registers twelve tools;
	`git_status` and `git_show` cover this. Bound to `E:\AI\iris` in `config.json`; other
	repositories need their own `mcp_servers` entry or the `repo_path` argument.
- [x] Commits and history (moved here from 3.2). `git_log`, `git_show`.
- [x] Branches. `git_branch` to list; `git_create_branch` and `git_checkout` go through confirm.
- [~] Diff and blame on demand. `git_diff`, `git_diff_staged`, `git_diff_unstaged` render as
	diff results (2.7). The reference server has no blame; that waits for a second server or a
	wrapped `git blame`.
- [ ] Pull requests: list, read, comment, create.
- [ ] Build status and pipelines.
- [ ] Work items: read, link to commits and PRs, update.
- [ ] Relate a work item or PR back to the project it belongs to (3.4).
- [ ] Eventually support full development workflow management.
- [~] **Decided 2026-09-10:** Iris's first two MCP servers (2.3) — `mcp-server-git` (the reference
	server, via `uvx`) and Microsoft's official `@azure-devops/mcp` (via `npx`). Both pass through
	the confirm/audit spine, so a commit or a work-item update still gets a preview. Read-only
	surfaces first. Git is running (above); Azure DevOps is not configured yet.
- [ ] **Decided 2026-09-10:** Azure DevOps authenticates through `az login` (Entra). Iris stores no
	secret; the CLI owns token refresh. `az` 2.87 is installed.
- [ ] Passed over: GitPython/pygit2 and hand-wrapping `az devops` — the servers already exist and
	are maintained by their owners.

### 3.4 Project and Task Awareness

- [~] Define what a project *is*: a named record with folders, repos, an ADO area path, documents,
	and people attached. **Built 2026-09-12:** `core/projects/service.py` owns
	`Data\Memory\projects.json` -- id, name, status, summary, technologies, `paths.workspace /
	repository / documents`, focus, decisions, tasks (`next_actions`). Every write is copied
	first through the change ledger, so `/undo` puts it back. An ADO area path and people are
	still to add; nothing reads them yet.
- [x] Track active projects. `/project list`, `/project <name>`, `/project new <name>`,
	`/project clear`; the active project is per session as before.
- [~] Associate conversations, files, code, decisions, and tasks per project. Sessions carry
	`project_id`; folders are linked with `/project link workspace|repository|documents <path>`;
	decisions and tasks live on the record (`/project decide`, `/project task add`, the
	`project_update` tool). Individual files and knowledge records are not tagged yet.
- [x] Switch projects explicitly, and infer the likely project from context (3.1). Inference
	(`ProjectService.infer`): the window's target path inside a linked folder, then the AL
	workspace in view (linked root or matching name), then the window title. `/project` says what
	is in view and why; `/project use` adopts it; the system prompt names the likely project
	without switching.
- [ ] Scope memory per project (2.1).
- [~] Remember current project state. Focus, decisions, open tasks and last-opened date are on
	the record and in the prompt when the project is active. What was being done in the last
	session is not summarised into it yet.
- [x] Track unfinished work. `/project tasks`, `/project task add <text>`, `/project task done
	<n>`, and the `active_project` / `project_update` tools; open tasks appear in the prompt.
- [~] Surface relevant prior decisions automatically. The active project's decisions are in the
	system prompt on every turn (`context_builder`), so the model can cite them; ranking by
	relevance to the request is not done.
- [ ] **Decided 2026-09-10:** where a project names an Azure DevOps area path, its tasks are a
	*view* over ADO work items through the ADO MCP server (3.3); otherwise Iris keeps a local list.
	One list per project, never two. The project record already has `paths.repository / workspace /
	documents` — empty on all three projects today — so linking is a data change, not a schema one.

---

## 4. Application Integration Layer

Goal: build adapters on top of existing automation APIs, not replacement applications.

- [x] **Built 2026-09-11 from a real session:** launching an unknown program no longer asks for a bare
	executable path. Iris scans the Start Menu shortcuts and the App Paths registry (cached, under a
	second), offers the matches as a choice, and falls back to a file picker. "Open <name>" now
	checks whether the name is a known folder (VS Code's recent workspaces, subfolders of the document
	roots) and offers to open it in VS Code, with a choice when several match. `launch_application`
	takes an optional target path. -> `core/system/applications.py`, `core/system/places.py`,
	prompt types CHOICE and FILE in `core/assistant/prompting.py`.

Every adapter follows the same shape, so a new app is a plugin (2.3) rather than a redesign:

	detect (3.1) -> read -> propose -> confirm (10) -> apply -> verify -> undo

An adapter is only as trustworthy as its undo. Read-only ships first in every case.

### 4.1 Excel

- [x] Detect active workbook — via 3.1, not a separate mechanism. **Built 2026-09-12:** the
	Excel tools take the workbook the context service saw (path, sheet, selection), then Excel's
	own active workbook, then a name of an open workbook, then a path on disk inside the document
	roots.
- [x] Inspect sheets, formulas, tables, named ranges, and pivot tables. **Built 2026-09-12:**
	`core/excel/live.py` over COM (`GetActiveObject`, never `Dispatch`): sheets with used-range
	size, ListObjects, pivot counts, hidden and protected flags, defined names, active sheet and
	selection, unsaved and read-only state. Tools: `excel_workbook`, `excel_read` (values and
	formulas of a sheet, range, table or name, as a table result), `excel_find` (cells containing
	text), `excel_check` (cells showing #REF!, #DIV/0!, #N/A and the rest, with the formula behind
	each). Error cells arrive from COM as integer codes and are mapped to their names.
- [x] Read a closed workbook without opening Excel. `core/excel/closed.py` over `openpyxl`, the
	same four tools. Error checking on a closed file needs Excel to have saved cached values; a
	file written by a library and never opened reports none, and the tool says so only implicitly
	(worth a note in the result later).
- [ ] Make controlled edits.
- [ ] Validate changes: recalculate, compare before and after, check for new errors.
- [ ] Add backup and undo protection (10).
- [~] Handle the awkward cases explicitly: unsaved changes, protected sheets, a workbook the user
	is actively typing in, files locked by OneDrive. Unsaved changes, read-only and protected
	sheets are reported; a locked file gives a clear message; a workbook being typed in is not
	detected yet (COM calls simply wait for Excel).
- [x] **Decided 2026-09-10, built 2026-09-12:** the live workbook is reached through Excel COM via `pywin32` (already
	in the venv; verified against Excel 16). Attach to the running instance with `GetActiveObject`,
	never `Dispatch`, or Iris starts a second hidden Excel.
- [x] **Decided 2026-09-10, built 2026-09-12:** closed files go through `openpyxl`. The hand-rolled `zipfile` +
	`ElementTree` extractor is replaced — shared strings, merged cells, cached values vs formulas,
	date serials, tables, and named ranges are exactly what a mature library has already handled.
	Passed over: `excel-mcp-server` (a thin community wrapper on openpyxl), pandas (heavier, and
	the wrong shape for cell-level work).
- [ ] Later options if needed: Office Scripts or Graph for SharePoint-hosted workbooks; Power Query
	for refresh.

### 4.2 VS Code

- [ ] Active workspace and project awareness — via 3.1.
- [ ] Active file awareness — via 3.1.
- [ ] Selection awareness — via 3.1.
- [ ] File editing.
- [ ] Build and compiler integration.
- [ ] Show Iris's output inside the editor, not only in the Iris window.
- [~] **Decided 2026-09-10, half built 2026-09-12:** no custom extension. VS Code is already an MCP
	client, and the server it needs exists now (2.3), so Iris's memory is callable from the editor
	today. What is still missing is the other direction -- 3.1's awareness of workspace, file, and
	selection -- which is why the items above stay unchecked. VS Code and Copilot call Iris's tools directly,
	and selection and diagnostics come back through VS Code's own MCP tooling. A TypeScript
	extension is revisited only if inline panels or diffs inside the editor prove necessary.

### 4.3 Adobe

Expect one adapter per app: Adobe has no single cross-app automation API. All four are installed
under `E:\Adobe\` (2026 releases; Lightroom Classic), found 2026-09-10 — `C:\Program Files\Adobe`
holds only the Creative Cloud shell, which is why an earlier check missed them.

- [ ] Photoshop integration path — `E:\Adobe\Adobe Photoshop 2026\`.
- [ ] Premiere Pro integration path — `E:\Adobe\Adobe Premiere Pro 2026\`.
- [ ] Lightroom integration path — `E:\Adobe\Adobe Lightroom Classic\`.
- [ ] After Effects integration path — `E:\Adobe\Adobe After Effects 2026\`; `aerender.exe` is
	present, so headless rendering is a subprocess call.
- [ ] **Decided 2026-09-10:** Photoshop first, batch-first — export, resize, rename, tag — through
	UXP scripting, the current supported route. Fine creative control is not the goal. After Effects
	via `aerender` is the natural second, being a subprocess call.
- [ ] Options: UXP plugins and the Photoshop scripting API (the current path for Photoshop);
	ExtendScript/CEP, legacy but still the route for Premiere Pro and After Effects;
	`aerender` for headless After Effects renders; the Lightroom Classic SDK (Lua) or reading the
	catalog directly; Adobe Bridge for batch; watched folders as the crude fallback.

### 4.4 3D and Animation

- [ ] Blender integration path (high value — full Python API). Installed at `E:\Blender\`:
	Blender 4.5.0 on an embedded Python 3.11.11 — the same minor as the Iris venv, so scripts and
	data types move between them without translation.
- [ ] Reallusion Character Creator integration path — CC4 and CC5 at `E:\Reallusion\`.
- [ ] Reallusion iClone integration path — iClone 8 at `E:\Reallusion\iClone 8\`.
- [ ] Reallusion ships its official Python API, `RLPy`, on an embedded Python 3.10 with dedicated
	launchers (`iClonepy.exe`, `CharacterCreatorpy.exe`). Scripts run inside the app, so the adapter
	is a script-runner plus a socket or file handoff, not an in-process import.
- [ ] Asset awareness: know what scenes, characters, and renders exist, and where (2.6).
- [ ] Render queue and progress monitoring (8.2).
- [ ] **Decided 2026-09-10:** Blender through the community `blender-mcp` server (1.9.1 — an
	add-on plus an MCP server), registered like git and ADO (2.3). Community-owned, so the version
	is pinned and the add-on is vetted before each bump. Headless `blender -b --python` stays for
	batch jobs that do not need a live scene.
- [ ] **Decided 2026-09-10:** Reallusion through `RLPy`, the vendor's supported API, in-house: Iris
	hands a script to the `*py.exe` launcher and reads the result back. No community server exists.
- [ ] FBX/USD/glTF remain the interchange path where an API falls short.

### 4.5 Microsoft 365

- [ ] Outlook and email access.
- [ ] Calendar access.
- [ ] Teams access.
- [ ] OneDrive access — storage behaviour is specified in 2.6.
- [ ] Search and retrieve files and messages; indexed content goes through 5.2.
- [ ] Understand project-related communication — which thread belongs to which project (3.4).
- [ ] Enable controlled actions later: send, reply, schedule, file.
- [ ] **Decided 2026-09-10:** Iris serves the work tenant (`elephas.us`), the identity `az` is
	already signed in with. Still to settle: app registration, delegated scopes, and whether tenant
	admin consent is available. This remains the most likely hard blocker in this section.
- [ ] Options: Microsoft Graph with MSAL device-code or interactive auth (the real path);
	Outlook COM as a local-only fallback; Graph change notifications for push; Exchange Web
	Services only if Graph cannot reach something.

---

## 5. Knowledge and Media Intelligence

### 5.1 Web Research and Media Discovery (High Priority)

Presentation moved to 2.7 and 6. This section is about getting good information.

- [~] Focused lookups already work for weather, news, stocks, time, and definitions
	-> `core/assistant/general_knowledge_router.py`. General research does not.
- [x] General web research. **Built 2026-09-12:** `web_search` (`core/actions/implementations/
	web_search.py`) over `core/web/search.py`, a client for the SearXNG JSON API -- query,
	category, optional time range, results deduplicated by URL and ranked by SearXNG's score,
	every hit cited with its URL and date, cached ten minutes. Read-only, outbound, no
	confirmation. Follow with `fetch_web_page` to read a hit.
- [x] Technical information search. Same tool; the model picks `general` and follows with a fetch.
- [x] Documentation research. Same tool.
- [x] Image search. `category: images` returns image results (thumbnail shown, the page opens on
	click) so the panel (6) shows a grid of pictures rather than a list of links.
- [x] Video search. `category: videos` returns link cards with the page and snippet; playback
	stays in the browser.
- [x] News and current information. `category: news`, with `time_range` for recency.
- [x] Fetch and extract page content, not just search snippets. -> `core/web/fetch.py`, the
	`fetch_web_page` tool. Built 2026-09-10.
- [ ] Compare multiple sources, and say when they disagree.
- [ ] Summarize findings, always with citations.
- [ ] Preserve useful findings in Iris memory when appropriate (2.1), with the source URL and
	the date retrieved.
- [~] Cache results, and respect rate limits and site terms. Page fetches are cached for ten
	minutes and throttled to one request per host per second; nothing reads robots.txt yet.
- [x] Return structured results per 2.7, so the UI can show thumbnails, previews, and cards.
	Search hits are link and image results; unresponsive engines arrive as a warning status.
- [x] **Decided 2026-09-10, running 2026-09-12:** general search through a self-hosted SearXNG container — keyless,
	web/images/videos/news in one JSON API, and only the query leaves the machine, under SearXNG's
	identity rather than yours. Docker Desktop 29.5 is installed (the engine was not running at
	review time; starting it is a phase step). It is the one long-running service Iris depends on.
	Passed over: Tavily and similar (LLM-backed server-side — no other AI, 1), `ddgs` (scrapes
	DuckDuckGo; ToS-grey and brittle), Brave Search API (clean, but keyed and identity-bearing;
	the fallback if the container ever proves a burden). **Running 2026-09-12:** `docker/
	docker-compose.yml` and `docker/searxng/settings.yml` (JSON format on, limiter off, bound to
	127.0.0.1:8888 only); Docker Desktop had to be switched to Linux containers. `web.search_url`
	in `config.json` overrides the address. When the container is down the tool fails with the
	compose command to start it, so the answer says what is wrong rather than guessing.
- [x] **Decided and built 2026-09-10:** fetch with `httpx`, extract with `trafilatura` (installed) — clean
	text, title, author, date. Every result cites the final URL and fetch time (2.7). Guards: http(s)
	only, no credentials in the URL, no loopback/private/link-local hosts (the LAN and Iris's own
	services are reached through their integrations, never through a model-chosen URL), 3 MB cap. Playwright deferred until JavaScript-only pages actually block work;
	it is a ~300 MB browser download.
- [ ] The five keyless endpoints the fixed providers already call — DuckDuckGo instant answers,
	Google News RSS, stooq, wttr.in, worldtimeapi — stay as they are.

### 5.2 Document Intelligence

- [x] Ingest PDFs, Word, Excel, CSV, and text. -> `core/documents/extractors/`
- [x] Search across documents. -> `core/documents/search_service.py`
- [x] Handle scanned documents and images of text (OCR). Built 2026-09-10 for PDF pages with
	no text layer -> `core/documents/ocr.py`; images on their own are not indexed yet.
- [ ] Handle PowerPoint, saved email (.msg/.eml), and Markdown.
- [~] Preserve the structure worth having: headings, tables, and page numbers for citation.
	Pages are marked `[page N]` in PDF text, Word tables render as rows, Excel as sheet/rows;
	headings are not distinguished from paragraphs.
- [ ] Extract and relate document content to projects (3.4).
- [~] Cite the exact location — file, page, sheet, cell — when answering from a document. A
	meaning match carries the passage and its page (or character offsets) in the search result;
	sheet and cell are not tracked.
- [x] Keep the index current as files change (2.6). Built 2026-09-11; the embedding pass picks
	up the re-indexed file on its next run.
- [ ] Later support editing and creating documents through proper integrations (4).
- [x] **Decided and built 2026-09-10:** the xlsx and docx extractors move from hand-rolled `zipfile` +
	`ElementTree` to `openpyxl` and `python-docx` (both installed). Headers, footers, tables, and
	footnotes in Word; cached values, merged cells, and date serials in Excel — the cases the
	hand-rolled parsers do not see.
- [x] **Decided and built 2026-09-10:** PDFs through `PyMuPDF` (installed), replacing `pypdf` — faster, real
	layout and tables, and it renders pages to images, which is the doorway to OCR.
- [x] **Decided and built 2026-09-10:** scans through Windows' built-in OCR engine via `winocr` (installed,
	no separate installer), with `qwen2.5vl:7b` — already pulled — as the local vision fallback for
	what OCR cannot read. Tesseract passed over as one more installer to maintain.
- [x] **Decided and built 2026-09-10:** document embeddings use the same `nomic-embed-text` as memory, in
	their own vector index beside `documents.db` (`usearch`). One model, two stores, each backed up with
	its own database. -> `core/documents/embeddings.py` over the shared `core/storage/vector_table.py`;
	chunked (1,200 chars, overlapping), fused into `DocumentSearchService` as a meaning bonus with
	the passage and page attached. `pypdf` can leave `requirements.txt` once the release venv is rebuilt.
- [ ] Options still open: docling or unstructured for mixed corpora; python-pptx; extract-msg.
	**Evaluated 2026-09-11:** Docling (IBM, local, real table and layout extraction, its own OCR,
	PPTX/EML/EPUB) is the upgrade when complex PDFs matter; deferred because it pulls in torch and
	models and PDF summaries are rare here. The other 2026-09-11 evaluations (Mem0, Letta, Zep,
	Graphiti, Semantic Kernel, LangGraph, AutoGen, LlamaIndex, Haystack, Cognee, txtai) are
	recorded on the 2.1 framework line and in 2.3.

---

## 6. User Interface and Experience

Rendering only. *What* gets rendered is defined in 2.7.

- [ ] Replace the current plain UI with a modern experience.
- [ ] Design the UI as the central interface for all Iris capabilities.
- [~] Render every result type from 2.7: rich response panels, image previews, video previews,
	syntax-highlighted code, diffs, file previews, tables, and search result cards. **Built
	2026-09-12, second pass:** file search results are cards in the web panel too, grouped by
	folder, each name opening the file and each folder heading opening the folder; the older tree
	panel now only shows indexing progress. **First pass,
	2026-09-12:** the detail panel is a `QWebEngineView` (`ui/results_panel.py`) showing HTML from
	`core/results/html.py`, one renderer per kind -- tables with numeric columns aligned, bar and
	line charts as inline SVG, diffs coloured by line, code blocks, file cards with Open and
	Folder, images and video served from disk, link cards with byline, status badges. The renderer
	is core code with no Qt in it, so the HTTP surface can serve the same page later. Still to do:
	syntax colouring inside code blocks (`pygments` is in the dev venv only through pytest; using
	it is a dependency decision).
- [x] Stream responses as they generate, and let the user stop a running request. Built
	2026-09-10: the coordinator streams the main answer through `on_delta`, the service turns
	fragments into `IrisEvent.delta`, the desktop window rewrites the placeholder bubble as text
	arrives, and Stop closes the model stream and keeps what came through.
- [~] Show tool and action status clearly — what is running, what it touched, what it cost.
	**Built 2026-09-12:** every response carries `metadata["activity"]` -- the request's actions
	and tools with status and target, model calls with model, tokens and wall time, permission
	refusals, total elapsed -- assembled from the same audit stream, metrics and log that `/why`
	reads (`WhyCommandHandler.summary`). The window shows it as one line under the header
	("Tools: al_symbol codeunit 80 Sales-Post (ok) · Model: qwen2.5-coder, 1 call, 750 tokens,
	1.5 s · Total 2.1 s") and "Working…" while a request runs. Per-tool progress *during* a
	request is not emitted yet.
- [x] Show pending confirmations and approvals prominently (10). **Built 2026-09-12:** while an
	action waits, an approval bar with Approve and Cancel sits at the top of the panel and the
	change itself is a card -- title, summary, target, the unified diff or the after-state, and
	whether it is copied first for `/undo` or cannot be undone. The executor now stamps
	`irreversible` on the pending preview and the service passes the preview's fields through
	`DetailContent.metadata["confirmation"]`, so any client can render the same card.
- [ ] Improve conversation and project organization (2.5, 3.4).
- [~] Make it reachable instantly: tray icon, global hotkey, a small always-available input.
	**Built 2026-09-12:** a tray icon (`ui/desktop_extras.py`) with Show, Hide to tray, "Close to
	tray instead of quitting" (remembered in QSettings) and Quit; a global hotkey, Ctrl+Alt+I by
	default (`window/hotkey` in QSettings), registered with `RegisterHotKey` and caught by a
	native event filter, that brings the window to the front and focuses the input. The small
	always-available input is not built.
- [ ] Keyboard-first navigation and a command palette.
- [~] Show sources, and let the user open the underlying file, page, or record in one click.
	**Built 2026-09-12:** every card shows its source and the time it was made; a web source opens
	in the browser and a path opens in its Windows default app through an `iris://open` link the
	panel intercepts, with no JavaScript enabled in the view. Records (memory ids) do not open yet.
- [~] Dark mode and readable defaults at the screen sizes actually used. The result panel
	follows the Qt palette (light and dark token sets, 2026-09-12); the rest of the window is
	stock Qt widgets.
- [~] **Decided 2026-09-10:** stay on PySide6, and render the result panels in an embedded
	`QWebEngineView` — `QtWebEngineWidgets` is already present in the installed PySide6 6.11.
	Streaming arrived with the `ollama` client change (2.4) and is wired (above); the web view
	arrived 2026-09-12 (above). A web front end on the FastAPI app was
	passed over: a second TypeScript codebase for a phone view that 11 does not ask for yet. The
	result contract (2.7) keeps that door open without touching core.

### 6.1 Voice and Interaction Modes

`core/voice/` exists as an empty package, so this was intended; it had no section. Local-only (1)
rules out every hosted speech API, which leaves a short, good list.

- [ ] Push-to-talk first; a wake word only if hands-free use is actually wanted.
- [ ] Speech to text on this machine.
- [ ] Text to speech on this machine, for replies and for notifications (8.2) read aloud.
- [ ] Voice is another client of the same core — the result contract (2.7) decides what a spoken
	answer contains; the voice layer only renders it.
- [ ] Barge-in: speaking over Iris stops the reply.
- [ ] Options — STT: `faster-whisper` (CTranslate2, runs on the 4060 Ti, best accuracy per second),
	`whisper.cpp` (CPU-friendly), Windows Speech Recognition (built in, weakest). TTS: `Piper`
	(fast, local, natural enough), Windows SAPI via `pywin32` (built in, robotic), `Kokoro`
	(higher quality, heavier). Wake word: `openWakeWord`. Audio I/O: `sounddevice`.
	Nothing here is decided; the VRAM budget in 2.4 has to include whichever STT model is chosen.

---

## 7. System, Network, and Infrastructure

### 7.1 System, PC, and Diagnostics

- [x] Disk usage visibility (for example: "What is eating space on E:?"). -> `disk_usage` tool.
- [~] Drive health (SMART), including the drives holding Iris data and camera footage.
	-> `drive_health` tool: Windows' health verdict per physical disk works unelevated; the SMART
	counters (temperature, wear, errors) need an elevated process and are shown when readable.
- [x] Memory and RAM usage. -> `system_overview` tool.
- [x] Process visibility. -> `top_processes` tool.
- [x] CPU and GPU utilization, including VRAM — Iris shares the GPU with its own models.
	-> `system_overview` (psutil + `nvidia-smi`).
- [ ] Temperatures and fan state.
- [x] Windows Event Logs. -> `event_log_errors` tool (System and Application, level error and critical).
- [x] Services status. -> `windows_services` tool.
- [x] Startup applications. -> `startup_apps` tool.
- [ ] Hardware information.
- [ ] Installed software and pending Windows updates.
- [ ] File-system tools.
- [ ] Prefer native Windows APIs/interfaces and proven open-source utilities.
- [ ] Expose all of this as declared tools (2.3), not ad-hoc shell calls.
- [x] **Decided and built 2026-09-10:** `psutil` (installed) for CPU, RAM, disk, processes, and network
	counters; `nvidia-smi` (present, driver 596) as a subprocess for GPU and VRAM; drive health from
	Windows itself — `MSStorageDriver_FailurePredictStatus` over WMI and
	`Get-StorageReliabilityCounter` for NVMe — with no installer; event logs, services, and startup
	items through PowerShell cmdlets. Passed over: smartmontools and LibreHardwareMonitor, each one
	more installer. Consequence to state plainly: CPU/GPU temperatures and fan speeds are not
	cleanly reachable on Windows without LibreHardwareMonitor, so that line stays unbuilt until it
	is wanted enough to install it.
	(Network diagnostics moved to 7.2, where it was already listed.)

### 7.2 Network and Infrastructure

- [ ] Network diagnostics: ping, DNS, traceroute, port checks, throughput.
- [ ] Device discovery.
- [ ] Connectivity troubleshooting.
- [ ] UniFi / Ubiquiti Dream Machine integration.
- [ ] Start read-only with:
	network status, connected clients, device health, logs, traffic/issues.
- [ ] Add controlled configuration changes later (10 applies).
- [ ] Watch for firmware updates and offline devices (8.2).
- [ ] NAS integration. Storage behaviour is specified in 2.6; this covers the device itself —
	health, volumes, SMART, temperature, backup jobs.
- [ ] **Decided 2026-09-10:** the gateway is a UniFi Dream Machine, so the adapter uses the
	official UniFi Network API on UniFi OS — an API key issued from the console, read-only
	endpoints for sites, devices, and clients. In-house over `httpx`. Passed over unless the
	official API proves too thin: `aiounifi` (the unofficial controller API wrapper Home Assistant
	uses), SSH, SNMP, syslog.
- [ ] **Decided 2026-09-10:** the NAS is a Synology, so device health comes from the official DSM
	Web API — login, `SYNO.Core.System` for volumes and temperature, `SYNO.Storage.CGI.Storage` for
	disks and SMART, backup task status. In-house over `httpx`; credentials in Credential Manager
	(10). SNMP and SSH stay as fallbacks. File access is unchanged: UNC path through `pathlib` (2.6).

---

## 8. Awareness, Monitoring, and Automation

### 8.1 Screen and Desktop Awareness

- [ ] Understand on-screen context when requested.
- [ ] Active window awareness — via 3.1.
- [ ] Optional screenshot and vision analysis.
- [ ] Read the UI tree where available before falling back to pixels — cheaper and exact.
- [ ] Use for apps without strong APIs.
- [ ] Prefer API/native integration whenever available.
- [ ] Capture is explicitly triggered, never continuous. Captures are retained per 2.8 and analysed
	locally, always (1).
- [ ] **Decided 2026-09-10, cheapest layer first:** `pywin32` (present) for the foreground window
	and owning process; the UI Automation tree for text and controls without pixels; `winocr`
	(installed) for screenshots; `qwen2.5vl:7b` (pulled) for understanding. No cloud vision (1).
- [ ] **Open:** `pywinauto` for the UI Automation tree. Without it the tree is raw `comtypes` calls,
	which is more code than the library saves. Not yet approved.

### 8.2 Notifications and Monitoring

- [x] Monitor conditions without repeated prompting. -> `core/watchers/`, `/watch`. Built 2026-09-10.
- [x] Define a watcher once — source, condition, threshold, channel — rather than coding each one.
	A watcher is data (`Data\Configuration\watchers.json`): a kind, its parameters, an interval,
	and channels. Ten kinds today: disk free, RAM, CPU, VRAM, service stopped, drive unhealthy,
	new event-log errors, path changed, path missing, host unreachable.
- [x] Watch PC health (7.1). -> disk_free_below, memory_percent_above, cpu_percent_above, vram_percent_above, drive_unhealthy, event_log_errors.
- [~] Watch network problems (7.2). -> host_unreachable (TCP port); nothing gateway-aware yet.
- [ ] Watch development builds and pipelines (3.3).
- [ ] Watch cameras. Camera-specific behaviour is in 9.1; this is the delivery side.
- [~] Watch files and folders (2.6). -> path_changed / path_missing by polling for notification
	watchers; the document index itself uses `watchdog` events (2.6).
- [x] Watch services. -> service_not_running.
- [ ] Watch long-running jobs Iris itself started: renders, indexing, scans.
- [x] Add trading-related monitoring later. Done 2026-09-12; the three kinds are listed in 13.1.
	Watcher checks can now be handed a context (the knowledge graph today), which is what let a
	watcher ask a question about memory instead of only about the machine.
- [x] Notify only on meaningful events: thresholds with hysteresis, deduplication, and suppression
	of a condition already reported. A transition notifies once, an ongoing condition at most every
	`renotify_minutes`, clearing is reported past a margin (disk: 10% above the limit; RAM: 5 points).
- [x] Respect quiet hours, and offer a "what did I miss" digest instead. `notifications.quiet_hours`
	in config (or `/watch quiet 22:00-07:00`); held toasts go to `/watch missed` and one digest toast
	at the end of the window.
- [x] **Decided and built 2026-09-10:** `APScheduler` (installed) is the scheduler — in-process. Its
	SQLite job store needs SQLAlchemy, which is not installed, so definitions and state live in Iris's
	own JSON and every watcher is rescheduled at startup: watchers survive a restart all the same. Iris had no scheduler at all; the only
	background machinery was cancel events and the UI's QThread. This also answers PhaseStatus's
	open question of what triggers TEST and MEASURE without a person. Passed over: Windows Task
	Scheduler (one process per job, no shared state), a hand-rolled asyncio loop.
- [x] **Decided and built 2026-09-10:** first delivery channel is Windows toast via `windows-toasts`
	(installed, native WinRT). Phone push (ntfy, Pushover), email, and Teams are not built until
	an away-from-desk need is real.

### 8.3 Workflow Automation

- [x] Chain tools into workflows. **Built 2026-09-12:** `core/workflows/`. A workflow is steps,
	each naming a tool from the registry (2.3) and its arguments; an argument can read the
	trigger (`{{trigger.url}}`) or an earlier step's result (`{{steps.fetch.first.text}}`, or a
	`save_as` name), so one step feeds the next without prose in between.
- [x] Support patterns like:
	detect event -> gather info -> analyze -> store result -> notify.
	That shape is a watcher trigger, `fetch_web_page`, the model or a capability, `observe` over
	MCP or the knowledge tools, and a notification -- each an existing tool. The test suite runs
	exactly that chain.
- [x] Triggers: manual, scheduled, and event-driven. `/workflow run`, a `schedule` trigger
	(cron or interval) that becomes a scheduled job (8.2) and follows the workflow when it is
	saved, changed, disabled or removed, and a `watcher` trigger naming a watcher id or kind, run
	with the alert as its payload when that watcher fires.
- [x] Handle failure explicitly: retry, timeout, partial completion, and what to do about each.
	Per step: `on_failure` is `stop` (the run fails there), `continue` (the run finishes
	`partial`) or `retry` (up to `retries`, backing off); `timeout_seconds` fails a step that does
	not return; every attempt and its time are in the run record; a failed, partial or blocked
	run lands in the inbox.
- [x] Support a human approval step mid-workflow (10). A step marked `approve` stops the run
	as `awaiting_approval` with everything before it kept; `/workflow approve <run>` runs it and
	the rest. A tool that asks for confirmation on its own does the same, so a workflow never
	confirms anything a person did not.
- [x] Dry-run a workflow before enabling it. `/workflow dry-run <id>` renders every step's
	arguments and asks the executor what it *would* do -- the target, the files it would write,
	the diff, whether it would ask -- and runs nothing. The same `executor.preview()` closes the
	2.3 item on dry-run for any tool that writes.
- [x] Store workflows as data — versioned, editable, and disableable. One JSON file per workflow
	under `Data\Workflows\`, edited by hand and picked up on change; the version bumps when the
	content changes; `enable`/`disable` keeps the file. Each file pins the version of every tool it
	uses, and a run is blocked -- not silently different -- when a tool has changed since, until
	`/workflow rebase` accepts the new versions. That is the 2.3 item on tool versioning, closed.
- [~] Add reusable workflows over time. `/workflow new <name> <tool> ...` writes a skeleton to
	edit. None shipped yet; the first candidates are the trading morning (13.1) and a page-to-memory
	clip.
- [x] Keep this built on the plugin/tool architecture (2.3), not as a separate automation stack.
	A step is a registry tool run through the same executor, permissions (10), change ledger and
	audit (2.8) as a spoken request; the host and the window build that tool set from one place
	(`core/actions/bootstrap.py`), so a workflow runs the same either side. Capability tools
	(weather, news) live with the model in the window; a workflow using them runs there.
- [x] **Decided 2026-09-10, built 2026-09-12:** in-house on the tool layer, with `APScheduler`
	(8.2) supplying the scheduled and interval triggers and event triggers coming from watchers
	(MQTT joins with 9.1). n8n and Node-RED passed over; a visual editor has not been asked for.

---

## 9. Cameras and Home Automation

### 9.1 Blue Iris and Camera Intelligence

- [ ] Keep Blue Iris as the NVR/recording layer initially.
- [ ] Integrate Iris above Blue Iris as intelligence.
- [ ] Consume Blue Iris motion/object triggers.
- [ ] On event:
	grab snapshot/clip -> analyze -> store metadata -> add to searchable vision index.
- [ ] Support semantic camera history search.
	Example: "Find times yesterday when people were in the living room."
- [ ] Return thumbnails and timestamps (rendered per 2.7).
- [ ] Support follow-up drill-in.
	Example: "Show me number 3." then play/open recording.
- [ ] Show camera status.
- [ ] Show recording status.
- [ ] Show storage health — reuse the drive checks in 7.1 rather than a camera-specific one.
- [ ] Alert on offline cameras (delivery per 8.2).
- [ ] Set retention for snapshots, clips, and the vision index, and keep all of it local.
- [ ] Revisit NVR replacement only long-term (not early scope).
- [ ] **Decided 2026-09-10:** Blue Iris runs on another machine; its web server is reachable on the
	LAN and an MQTT broker exists. Pull through the JSON API (`/json`: login, cameras, alerts,
	clips, snapshots) and receive alerts by MQTT rather than polling. In-house over `httpx`, with
	`paho-mqtt` (installed) as the MQTT client — the same listener later carries Home Assistant
	events (9.2).
- [ ] **Decided 2026-09-10:** the vision index is `qwen2.5vl:7b` describing each alert frame and
	`nomic-embed-text` embedding the description, in `sqlite-vec` — "find times when people were
	in the living room" is a text search over descriptions. A CLIP-style image embedding (a second
	model, ~600 MB) is added only if "find this object again" becomes a real need. Plate or face
	recognition only if chosen deliberately.

### 9.2 Home Automation and IoT

- [ ] Integrate with existing home automation platforms.
- [ ] Evaluate existing interfaces before rebuilding.
- [ ] Build device awareness for sensors, lights, cameras, presence, and events.
- [ ] Let the platform own the devices and have Iris reason above it — the same split 9.1 uses
	for Blue Iris.
- [ ] Later enable cross-system reasoning.
	Example: "When X happens and nobody is home, do Y."
- [ ] Require controlled permissions for physical-world actions (10), with rate limits and an
	obvious way to stop everything.
- [ ] **Decided 2026-09-10:** Home Assistant is the intended hub. It is not running today but can
	be; when 9.2 starts it runs in Docker beside SearXNG (5.1), owns the devices, and Iris talks to
	its REST/WebSocket API. The MQTT broker that already serves Blue Iris (9.1) is the shared bus.
	Vendor clouds, Matter, and Zigbee2MQTT are Home Assistant's concern, not Iris's.
- [ ] **Open:** which devices are actually in the house today — a Home Assistant install answers
	that by discovery rather than by list.

---

## 10. Permissions, Safety, and Change Control

**Owner:** all permission and safety rules. Sections 2.3, 4, 7.2, 8, and 9.2 declare what they
need; this section decides what is allowed.

- [x] Enforce read vs write vs execute boundaries. **Built 2026-09-12:** `core/permissions/`
	decides, `core/actions/executor.py` asks before it runs anything, and the agent asks for the
	tools that do not go through the executor. Each level is `allow`, `confirm`, or `deny` in
	`permissions` in config.json. The shipped defaults leave today's behaviour exactly as it was
	-- all three allowed, each tool's own `requires_confirmation` unchanged -- because a
	permission layer that silently starts asking about every file open is a regression, not a
	feature. Tightening a level is one word in config.
- [x] Scope permissions to targets, not just verbs: allowlisted paths, repos, hosts, devices.
	**Built 2026-09-12:** `permissions.allowed_paths` / `denied_paths` / `allowed_hosts` /
	`denied_hosts`. Paths and hosts are read out of the resolved arguments, so a rule covers a
	tool written next year without naming it. Denied beats allowed; a host rule covers its
	subdomains and cannot be fooled by `example.com.evil.net`. Empty lists mean the action's own
	roots still apply and nothing else changes. Devices arrive with 9.2.
- [x] Require confirmation for dangerous actions. Declared per tool (`requires_confirmation`),
	raised per level by the policy (`permissions.write: confirm`), and since 2026-09-12 the
	confirmation says which of two things it is: a file write that is copied first so `/undo` can
	put it back, or an act that cannot be undone.
- [x] Show exactly what will change before confirming — diff, file list, target device. **Built
	2026-09-12:** a config change previews as a unified diff of the section it touches, a profile
	change as a diff of `profile.json`; the file about to be written is named in the preview.
	Devices arrive with 9.2. -> `core/actions/diffs.py`.
- [x] Backup before destructive file changes. **Built 2026-09-12:** an action names the files it
	will write (`ValidationResult.changes`) and the executor copies each one into
	`Data\Backups\undo\<change>\` before running it, keeping the last fifty changes. Every
	config operation and the profile update declare theirs; a new file-writing action gets this by
	naming its file. -> `core/actions/changes.py`.
- [x] Provide undo/rollback where possible, and say plainly when an action is irreversible.
	**Built 2026-09-12:** `/undo` puts the newest change back (`/undo <id>` a specific one,
	`/changes` lists them), a file that did not exist before is removed again, and an undo is
	itself audited. A tool declares `irreversible=True` (the clipboard does: what was on it is
	gone) and its confirmation ends with "This cannot be undone."; the rest end with "copied first,
	so /undo can put it back", so the difference is said before the choice is made.
- [x] Log all changes Iris makes (2.8). One stream since 2026-09-12, tools and permission
	decisions included.
- [x] Show diffs. In the confirmation before a write (above), and as a result kind (2.7) any
	tool can return.
- [x] Keep credentials and secrets separate from model reasoning: never in `config.json`, never in
	a prompt, never in a log. **Built 2026-09-12:** `core/permissions/secrets.py` over Credential
	Manager, with the names (never the values) kept in `Data\Configuration\secrets.json` so
	`/secrets` can list what is set without reading any of it, and redaction on the audit stream
	(2.8) for anything that slips into an argument or a message.
- [x] Rate-limit and cap physical-world and outbound actions: email, messages, devices.
	**Built 2026-09-12:** a tool declares `outbound=True` on its definition (2.3) and the policy
	caps the lot together -- 120 an hour by default, a sliding window, configurable per bucket.
	Declared rather than a list of tool names in the policy, which would go stale the first time
	someone adds a tool. Web fetches and the knowledge providers are outbound today; opening a
	browser tab is not, since the browser is the thing making the call.
- [x] Authenticate the HTTP surface. **Built 2026-09-12:** one middleware in
	`core/server/app.py` over `core/server/auth.py`, so it is every request rather than every
	route someone remembers to decorate. A per-client token (`api_token:<client>`) out of
	Credential Manager, compared in constant time, accepted as `X-Iris-Token` or a bearer header;
	non-loopback callers are turned away before the token is even read. The check turns itself on
	the moment a token exists and says so at startup while none does, so installing it cannot
	lock out the desktop app or the bot mid-session; `http.require_token: true` demands one
	outright. `/health` stays open for monitoring. Refusals are audited without the token.
- [x] Provide a global stop that halts running tools, workflows, and watchers at once. **Built
	2026-09-12:** `/stop` cancels the pending confirmation and the running request, pauses the
	watchers and the scheduled jobs (they remember it across a restart), and halts the permission
	policy so every tool -- native, MCP, capability -- is refused with "/resume to continue" until
	`/resume`. When the Iris service is running, the window also calls its `POST /control/stop`, so
	one command reaches both processes. Workflows (8.3) will run under the same latch when they
	exist.
- [~] Nothing leaves the machine by default — decided 2026-09-10. There is no cloud AI (1, 2.4);
	any non-AI outbound call (search, Graph, notifications) names what it sends, and memory,
	documents, and camera content are never part of it. Since 2026-09-12 an outbound tool is
	declared as such, capped, and host-scoped, and `/permissions` shows the cap; "names what it
	sends" is still per tool and not yet enforced by the policy.
- [ ] Increase rigor as Iris shifts from advisory to action-taking behavior.
- [x] **Decided 2026-09-10, built 2026-09-12:** secrets live in Windows Credential Manager through
	`keyring` (installed; it selects the Windows backend itself, no service). `/secrets list|set|clear`
	manages them; where there is no credential store (a dev box, a headless run) a secret can come
	from `IRIS_SECRET_<NAME>` in the environment instead, and reading refuses silently rather than
	inventing a value. First tenants: the HTTP token above, then the UniFi API key, the Blue Iris
	login, the MQTT credentials, later Graph tokens. Passed over: raw `win32cred` (more code for the
	same store), an encrypted file, `.env`.

---

## 11. Runtime and Operations

How Iris runs day to day. None of this was in the original document, but every item is already a
real thing in the repo: `Launch-Iris.cmd`, `deploy_phase4.ps1`, `package_release.ps1`, `serve.py`,
and the `Data/` tree.

- [x] **Decided 2026-09-10, built 2026-09-12:** a headless host runs the parts that must run when
	no window is open -- the watchers, the scheduled jobs, the HTTP surface -- and the Qt window is a
	client of it. `core/host/service.py` (`IrisHost`) holds the knowledge layer, watchers, schedules,
	backups and uvicorn in one process with no model chat loop and no window; the MQTT listener
	joins it when 9.1 starts. `iris_service.py install|start|stop|remove` wraps it in
	`win32serviceutil` from the pinned `pywin32` (no new dependency; `install` sets automatic
	start and `sc failure` restart-on-crash), and `iris_service.py --console` /
	`Run-Iris-Service.cmd` runs the same host in a window on any machine, which is how it is
	tested. The desktop probes `/health` at startup and, when the host answers, does not start its
	own watchers or schedules and says so in its first lines. Watcher and schedule definitions are
	re-read when their files change, so `/watch add` in the window takes effect in the service
	within its heartbeat rather than at its next restart. Toasts are not a service channel --
	session 0 has no desktop -- so the host delivers to the inbox and the log and the window
	shows what arrived.
- [~] Start with Windows, survive a reboot, and recover from a crash. The service installs with
	automatic start and a restart-on-failure policy (5 s, 30 s, 60 s, reset daily), which is the
	Windows half. Still to verify on the machine itself: that the first boot after
	`iris_service.py install` brings the host up before the desktop opens.
- [ ] **Decided and done 2026-09-10:** one `requirements.txt` at the root for dev (`.venv`) and
	release (`Runtime\venv`). `core/requirements.txt` and `ui/requirements.txt` are removed and
	`deploy_phase4.ps1` now installs from the root file — before this, the release build read
	`core/requirements.txt`, which listed only `pypdf`, and would have missed every dependency
	decided today. `pywin32` is pinned explicitly rather than left transitive, since Excel COM,
	the service host, and 3.1 all depend on it.
- [~] One documented way to launch dev, one for release. Dev: `Run-Iris-Dev.cmd` ->
	`dev_run.ps1 -Mode ui|console|tests` (`-Bootstrap` creates `.venv`). Release:
	`deploy_phase4.ps1` then `Launch-Iris.cmd`. **Fixed 2026-09-12:** `dev_run.ps1 -Bootstrap` was
	still installing from `core\requirements.txt` and `ui\requirements.txt`, both removed on
	09-10, so a fresh dev box could not bootstrap; it reads the one root file now. Still to do: the
	service host's install and start belong in the same two scripts.
- [~] Config: one schema, validated on load, clear errors, no secrets (10). **Fixed 2026-09-12:**
	`ConfigLoader.load()` returned only the keys it normalized, so five configured sections --
	`mcp_servers`, `notifications`, `knowledge`, `web`, `metrics_path` -- were read from
	`config.json` and then silently dropped before anything could use them. The git MCP server
	(2.3) never started and quiet hours (8.2) never applied, both from this. They are passed
	through now, along with `permissions` and `http` (10). A real schema is still to come.
- [~] Document the data directory layout, and add migrations for the SQLite databases. The layout,
	as of 2026-09-12 (every path configurable, these are the defaults under `Data\`):
	`Memory\` (profile JSON, `conversations.db`, `knowledge.db` and its `.usearch` indexes),
	`Sessions\` (session JSON, `agent_checkpoints.db`), `Proposals\`, `Audit\` (`audit.jsonl`,
	`notifications.jsonl`, the two pre-unification files as history), `Index\documents.db` and
	its `.usearch` index, `Metrics\metrics.db`, `Configuration\` (watchers, schedules, secrets
	index, synonyms), `Backups\<stamp>\`, `logs\iris.jsonl`. Migrations: `knowledge.db` has the
	`SCHEMA_VERSION` ladder with a copy taken first; the other three create-if-missing and have
	not needed a migration yet.
- [x] Back up `Data/` — memory, sessions, audit, index — and test a restore. **Built 2026-09-12:**
	`core/storage/backups.py` writes one dated run under `Data\Backups\` -- the four databases
	(knowledge, conversations, documents, metrics) through the backup API, each copy re-checked;
	Memory, Sessions, Configuration and Audit as folder copies, with live `.db` files skipped there
	because a plain copy of an open database can be torn -- and keeps the newest seven
	(`backups.keep`). Runs daily at 03:00 as a scheduled job (8.2) and on demand with
	`/backup now`. `/backup restore [run]` puts a run back: the live files are set aside as
	`.before-restore-<stamp>` rather than overwritten, stale WAL sidecars are removed so SQLite
	cannot replay an old journal onto the restored file, and a copy that fails its own quick_check
	is refused. The restore is a test, not an assumption: `core/tests/test_storage_durability.py`
	deletes the rows, restores, and reads them back.
- [x] SQLite durability for `knowledge.db` and `conversations.db` — decided 2026-09-10, **built
	2026-09-12**, stdlib only, in the one wrapper every database already goes through
	(`core/storage/sqlite_database.py`): `journal_mode` is WAL with `synchronous=NORMAL` (Data/ is
	on a local, unsynced drive, so WAL is safe); `PRAGMA quick_check` runs on the first connection
	and a database that fails it is opened read-only from then on -- reads still answer, writes
	raise, the log says which file and why; `backup_to()` takes a consistent online copy with
	`Connection.backup()`, checkpointed into a self-contained DELETE-mode file. `knowledge.db` is
	copied beside itself as `knowledge.before-vN.db` before its `SCHEMA_VERSION` ladder moves, and
	never for a fresh file. Alembic/yoyo would still be more machinery than problem.
- [ ] An update mechanism that does not lose data or config.
- [ ] Resource limits: do not hold the GPU or thrash the disk while the user is working.
- [x] Health check: what is up, which model is loaded, what is indexed, what is broken. **Built
	2026-09-12:** `/health` on either host now says which host answered, the configured model and
	which routes are pulled (from the router's cached view -- a health check that waits on Ollama's
	full timeout is not a health check), record, vector and document counts, quick_check per
	database, watcher and schedule state, the newest backup, and a `broken` list; `status` is
	`degraded` whenever that list is not empty. -> `core/host/health.py`, one builder for the
	service, the desktop, and the MCP server.
- [ ] **Open:** is Iris ever reachable from a phone or another machine? That answer drives the UI
	choice in 6 and the auth story in 10.

---

## 12. Evaluation and Quality

Without this section, "Iris got better" stays an opinion.

**Reviewed 2026-09-10: no third party warranted.** `pytest` is present, and `comparison.py`'s
hit-rate-at-top-N is already an eval harness for the knowledge layer; the routing and retrieval
eval is a saved-requests fixture plus a scoring script in the same suite.

- [x] Automated test suite. -> `core/run_tests.py`, `core/tests/`
- [x] House rules enforced mechanically. -> `tools/house_rules.py`, `.pre-commit-config.yaml`
- [ ] A saved set of real requests, used to check routing, retrieval, and answer quality before
	and after a change.
- [ ] Measure retrieval quality directly — does the right memory come back? — not by feel.
- [x] A latency budget per request class, and a warning when it is exceeded. **Built
	2026-09-12:** `latency_budget_ms` in config (defaults: chat 8 s, code 20 s, intent 2.5 s,
	decision 6 s, summary 12 s, embedding 2 s) checked against the metrics table's per-class
	averages -- `/models budget [hours]` says which class is over, and `/health` lists it under
	`broken`. A class with fewer than three calls is not judged. -> `core/llm/budget.py`.
- [ ] Record failures in normal use — wrong tool, wrong answer, bad context — and feed them into
	2.2 instead of losing them.
- [ ] Regression check before swapping a model or changing a routing rule (2.4).

---

## 13. Applications Built on Iris

Systems that consume Iris rather than extend it. They enter through the HTTP surface and the
knowledge layer (2.1, 2.2), never through a private path. This section existed only as "deferred"
in Appendix A, which understated it: the first application is already wired.

### 13.1 Trading Bot

`E:\VS\Zuraw\StandardTradingBot` — `bot/api/iris/client.py` posts observations and calls
`/assess`, raising `IrisUnavailable` cleanly when Iris is down. PhaseStatus §7 records the Iris
side: batch observations, outcomes, recall, assess, compare.

- [x] Inbound path: the bot sends candidates as observations and closes them with outcomes.
- [x] Iris scores a cohort (`/assess`, recorded at the time so it cannot grade itself later).
- [x] Comparison: hit rate at top N, Iris against the bot, lift over base rate (`/compare`).
- [ ] Iris as the decision layer — the bot *acts* on Iris's assessment rather than logging it
	beside its own. Gated on the comparison showing sustained lift on real mornings, not seeded
	data; PhaseStatus is explicit that nothing has met real data yet.
- [x] Re-evaluate hypotheses on a schedule (APScheduler, 8.2) instead of only when a person files
	evidence. **Built 2026-09-12:** `core/scheduler/` holds jobs the same way 8.2 holds watchers --
	as data in `Data\Configuration\schedules.json`, rescheduled at startup -- and ships one:
	`hypothesis_review` re-appraises everything unsettled at 06:00 daily and raises a toast only
	when something newly has the evidence to be accepted. It still cannot accept anything: the
	schedule moves a hypothesis to supported, and a named person promotes it. Every run is audited
	(2.8). `/schedule` lists, runs, adds and disables them.
- [x] Trading monitoring (8.2): candidate-list health, outcome latency, an assessment that stops
	arriving. **Built 2026-09-12:** three watcher kinds over the knowledge graph --
	`no_new_records` (the candidate list stopped arriving), `outcomes_overdue` (observations sitting
	open past a deadline), `no_assessments` (Iris stopped scoring a topic it is supposed to score,
	counting only records Iris itself wrote, since the comparison depends on that).
	-> `core/watchers/knowledge_checks.py`.
- [x] Authenticate the bot's calls (10). Set `api_token:bot` on this machine and give the bot the
	same value; until one is set the surface stays open exactly as it was.

### 13.2 Later Applications

Anything else that wants Iris's memory and judgment — a BC assistant surfaced inside VS Code (4.2),
Home Assistant automations that ask Iris before acting (9.2) — follows the same shape: HTTP or MCP
in, result contract out, permissions in 10.

---

## Appendix A. Open Questions

Collected from the sections above. Each one changes the shape of the work that follows it.

1. ~~**Embeddings** (2.1, 5.2)~~ — closed 2026-09-10: `nomic-embed-text` + `sqlite-vec` for both;
	memory vectors in `knowledge.db`, document vectors in `documents.db`.
2. ~~**MCP** (2.3)~~ — closed 2026-09-12: a native registry with `mcp` as a client inside it, native
	tool-calling for selection, and Iris exposed as an MCP server over stdio (`mcp_server.py`), which
	is what 4.2 was waiting on.
3. ~~**Cloud escalation** (2.4, 10)~~ — closed 2026-09-10: no cloud AI, by principle. Nothing
	leaves the machine by default.
4. ~~**VS Code** (4.2)~~ — closed 2026-09-10: Iris as an MCP server; VS Code is already a client.
5. **Microsoft 365** (4.5) — tenant decided (work, `elephas.us`). Still open: app registration and
	admin consent.
6. ~~**UI stack** (6)~~ — closed 2026-09-10: PySide6 with `QWebEngineView` result panels.
7. ~~**Tasks** (3.4)~~ — closed 2026-09-10: a view over ADO work items where a project is linked,
	Iris-local otherwise.
8. **Home automation** (9.2) — Home Assistant is the aggregator (decided; not yet running). Still
	open: which devices are in the house — discovery will answer it.
9. **Reach** (11) — single machine only, or phone and remote access?
10. **Trading** (13.1) — the integration is built and scoring; Iris as the *decision* layer waits
	on sustained lift over real mornings. The gate is measured, not argued.
