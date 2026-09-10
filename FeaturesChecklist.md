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
| Local model access | Ollama only (`qwen3:8b`), no router | `core/llm/ollama_client.py` |
| Memory and knowledge layer | Built: facts, observations, outcomes, decisions, hypotheses | `core/knowledge/` |
| Learning loop | Built: evidence, appraisal, promotion gate, review queue | `core/knowledge/hypotheses.py`, `review.py`, `appraisal.py` |
| Conversation, session, topic state | Built | `core/conversation/` |
| Document indexing and search | Built for pdf/docx/xlsx/csv/text | `core/documents/` |
| Actions and tools | Fixed set: open file/folder/url, launch app, clipboard, config/profile | `core/actions/implementations/` |
| Capability/provider routing | Built for a fixed provider list (weather, news, stocks, time, lookup) | `core/assistant/general_knowledge_router.py` |
| Intent routing and planning | Built | `core/assistant/intent_router.py`, `action_planner.py` |
| Audit log | Memory changes and actions only, not general | `core/audit/logger.py` |
| Request tracing | Built, optional | `core/conversation/request_trace.py` |
| Desktop UI | PySide6 desktop app | `ui/main.py` |
| HTTP surface | FastAPI | `serve.py`, `core/server/` |
| Voice | Empty package, nothing built | `core/voice/` |
| Generic tool/plugin registry | Not built (the package is empty) | `core/tools/` |

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
- [~] Store project-specific knowledge. Records exist; project scoping does not (3.4).
- [x] Retrieve the right memory at the right time. -> `core/knowledge/retrieval.py`
- [x] Record provenance on every memory: source, time, status.
- [ ] Scope memory explicitly — global vs project vs session — and decide what a new project inherits.
- [ ] Handle conflicting or superseded facts: which one wins, and whether the old one is kept.
- [ ] Decide a forgetting policy: expiry, decay, relevance pruning, or never forget.
- [ ] Let the user browse, correct, and delete memories directly, not only through conversation.
- [ ] Export and back up memory in a readable format (11).
- [ ] **Decided 2026-09-10:** add embedding retrieval beside FTS5, not instead of it.
	Embeddings from `nomic-embed-text` via Ollama; vectors stored in `knowledge.db` with `sqlite-vec`,
	so they share the store's transactions and backup. FTS5 and vector KNN are fused, then the
	existing scorer (recency, prior, diagnostics) applies unchanged. `core/knowledge/` otherwise stays.
	Considered and passed over: LanceDB (pyarrow), Chroma (onnxruntime, separate directory),
	FAISS/hnswlib (separate index files to keep in sync), Mem0/Letta/Zep (server-shaped, and they
	solve conversational recall, which is not the slice that needs help).
- [ ] Embed everything except bot-generated observations — those are near-identical prose and are
	already matched by numeric features in the appraisal.
- [ ] Load-test retrieval at ~400k records (a year of bot volume) before trusting it at scale.
	sqlite-vec is brute-force KNN with no ANN index; this is where that either holds or does not.
- [ ] Evaluate memory/orchestration frameworks before extending custom code:
	Mem0, Letta, Zep, Graphiti, Semantic Kernel, LangGraph, AutoGen, LlamaIndex, Haystack.
	(2.2 references this list rather than repeating it.)

### 2.2 Decision-Making and Learning

- [x] Build the rational decision and learning layer. -> `core/knowledge/`
- [x] Store decision records with:
	situation and context, options considered, decision made, reasoning, outcome.
- [x] Never auto-promote a hypothesis; promotion is a human gate. -> `HypothesisTracker.promote`
- [ ] When Iris is corrected, capture why the correction was made.
- [ ] Turn repeated corrections into reusable principles.
- [ ] Keep those principles in a list the user can read, edit, and switch off.
- [ ] Close the loop automatically where the outcome is observable without being told —
	a build failed, a file was reverted, an answer was rejected.
- [ ] Learn BC architecture and design preferences over time.
- [ ] Say what it does not know, and record the gap as an open question rather than guessing.
- [ ] Framework evaluation: use the single list in 2.1.

### 2.3 Plugin and Tool Architecture

- [ ] Make plugin and tool architecture a core feature early. `core/tools/` is still empty;
	today's actions are a fixed set in `core/actions/implementations/`.
- [ ] Define a standard interface for adding tools.
- [ ] Let tools advertise their capabilities: name, purpose, typed arguments, cost, side effects.
- [ ] Let Iris choose appropriate tools.
- [ ] Declare each tool's permission level — read, write, execute. Enforcement lives in 10.
- [ ] Support dry-run / preview for any tool that writes.
- [ ] Give tools a uniform error and timeout contract, and make long-running tools cancellable.
- [ ] Log every tool invocation and result (2.8).
- [ ] Version tools, so a saved workflow (8.3) does not silently change meaning.
- [ ] Let a tool be disabled or sandboxed without removing it.
- [ ] Ensure future tools can be added without redesign — a new app integration (4) should be a
	plugin, not a core change.
- [ ] **Decided 2026-09-10:** one tool definition — name, description, arguments as a pydantic
	model (JSON Schema for free), permission level, `requires_confirmation`. Native actions and the
	knowledge providers (`CapabilityDefinition`, already this shape) both become it. The registry is
	the only place a tool is declared; today adding one touches four.
- [ ] **Decided 2026-09-10:** native Ollama tool-calling replaces the hand-written intent prompt in
	`_classify`. Verified on `qwen3:8b`: right tool, right arguments, no call for ordinary chat,
	under a second. Synonyms and learned examples stay as the fast path in front of it.
- [ ] **Decided 2026-09-10:** adopt the official `mcp` Python SDK as a *client*. MCP servers register
	into the same registry and pass through the same validate → preview → confirm → audit spine in
	`core/actions/executor.py`, which stays. Node 22 and `uv` are installed for launching servers.
	Passed over: LangGraph, Semantic Kernel, pydantic-ai, smolagents — each wants to own the loop,
	and Iris has an orchestrator.
- [ ] Later, once the client side works: expose Iris as an MCP *server*, so Claude Code and VS Code
	can call recall/observe/hypothesize directly. Cheapest route to 4.2.

### 2.4 Model Router

**Decided 2026-09-10: Iris runs on local models only.** No cloud AI provider, now or as a
"later" item — it is a principle of the project (1), not a deferral. The router's job is to choose
among local models, not to decide when to leave the machine.

- [ ] Do not permanently tie Iris to one model (for example, Qwen 8B). Currently hard-tied via
	`config.json -> model`.
- [ ] Support multiple specialized local models: chat, code, embedding, vision. Already pulled:
	`qwen3:8b`, `qwen2.5-coder:7b`, `qwen2.5vl:7b`, `mistral-small:24b`, `nomic-embed-text`.
- [ ] Route on explicit signals: task class (the nine call sites are seven classes — chat, summary,
	follow-up, topic patch, intent, decision, proposal), required context size, latency budget,
	and VRAM (the RTX 4060 Ti has 16 GB; two large models do not fit at once).
- [ ] Fall back cleanly when a model or host is down.
- [ ] Require structured output / tool-calling support from any model used for planning.
- [ ] Track tokens and latency per request, and make it visible (2.8).
- [ ] **Decided 2026-09-10:** replace the hand-rolled urllib client with the official `ollama`
	Python package — chat history, tools, streaming, and embeddings in one thin dependency.
	Today `stream: False` is hard-coded, which is why 6 cannot stream.
- [ ] **Decided 2026-09-10:** a thin in-house router behind the existing `LLMClient` seam. The
	seam itself must grow from `(system, user) -> str` to a request/response that carries messages,
	tools, and usage — that is the real work here.
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
- [ ] Know which roots are indexed, and keep the index current as files move.
- [ ] **Decided 2026-09-10:** `watchdog` for change detection — native Windows change
	notifications, one dependency serving this item and 8.2's file watchers. Scheduled rescans stay
	as the safety net, because a watcher can miss events during downtime.
- [ ] Respect path allowlists and per-root permissions (10).

### 2.7 Result Contract

**Owner:** the shape of an answer. Moved out of 5.1 and 6, which both described it.

- [ ] Tools and providers return structured results — typed data plus display hints — never
	pre-formatted prose.
- [ ] Define the result types once: text, table, image, video, file, code, diff, link/card,
	chart, status.
- [ ] Every result carries its source and timestamp, so it can be cited and re-run.
- [ ] The UI (6) renders these types. Any client — desktop, web, voice — renders the same
	results in its own way.
- [ ] Results can be saved into memory (2.1) or attached to a project (3.4) as they are.
- [ ] Map to and from MCP content blocks (text, image, resource), so an MCP tool's result (2.3)
	renders natively and an Iris result can be returned to an MCP caller later. In-house; pydantic
	is already the wire layer in `core/server/models.py`.

### 2.8 Observability and Audit

- [~] Log what Iris does. Exists for memory changes and actions -> `core/audit/logger.py`;
	not yet uniform across tools, models, and integrations.
- [~] Trace a request end to end: intent, plan, tools called, model used, timing.
	-> `core/conversation/request_trace.py`, optional today.
- [ ] Answer "why did you do that?" from the trace, in the UI, without reading log files.
- [ ] **Decided 2026-09-10:** `structlog` on top of stdlib `logging` — a request id bound once and
	present on every line, while `mcp`, `uvicorn`, and other libraries still log through stdlib and
	get captured. JSON lines, rotating file handler, size cap. Today: two `getLogger` sites, no
	prints, and the request trace wired into the coordinator only.
- [ ] **Decided 2026-09-10:** metrics are a SQLite table of per-request rows — model, tokens,
	latency, tools called, outcome — queried by the UI. No metrics server. OpenTelemetry was
	passed over: the API package arrived with `mcp`, but a useful setup means an exporter and a
	Jaeger/Grafana backend, which is three services for one user on one machine.
- [ ] Unify the two audit streams (`core/audit/logger.py` for memory, `core/actions/audit.py` for
	actions) into one schema; the tool registry (2.3) then writes every invocation there.
- [ ] Never log secrets or credential values (10).
- [ ] Retention policy for traces, audit entries, and captured screen or camera data.

---

## 3. Context and Developer Workflows

### 3.1 Active Context Awareness

**Owner:** "what is the user looking at right now." Sections 4.1 and 4.2 previously repeated
this; they now consume it.

- [ ] Understand what the user is currently working on.
- [ ] Detect active application.
- [ ] Detect active file, document, and project.
- [ ] Detect the current selection where the app exposes one.
- [ ] Handle requests like:
	"Look at the workbook I have open."
	"Look at the project I have open in VS Code."
	"Look at this file."
- [ ] Implement this generically: a context-provider interface with one small provider per app,
	not one-off handling per request.
- [ ] Resolve "this" and "here" against the current context, and say which target was picked.
- [ ] Keep a short history of recent context, so "the file I had open before" works.
- [ ] Context capture is user-visible and can be paused (10).
- [ ] Options: Win32 foreground-window APIs, UI Automation, per-app adapters (Excel COM, a VS Code
	extension), window-title parsing as the crude fallback.

### 3.2 Coding and Development Assistant (Start with BC/AL)

Git-specific items moved to 3.3.

- [ ] Begin with read-only repository access.
- [ ] Provide repository search — text first, symbol-aware later.
- [ ] Provide symbol and object awareness.
- [ ] Build relevant context automatically before model calls.
- [ ] Understand relationships between AL objects: tables, pages, codeunits, extensions, events.
- [ ] Read `app.json`, `launch.json`, and `.alpackages` to know the app, its dependencies, and target.
- [ ] Learn preferred BC patterns and architecture (feeds 2.2).
- [ ] Add controlled file editing later.
- [ ] Run the BC compiler and read errors.
- [ ] Correct its own changes.
- [ ] Run tests and read the results.
- [ ] Show code diffs before significant changes — rendered per 2.7, gated per 10.
- [ ] Target a VS Code-class coding experience.
- [ ] **Decided 2026-09-10:** symbol and object awareness comes from parsing `SymbolReference.json`
	inside the `.alpackages` files every AL project carries — in-house, no dependency. Verified on
	a BC 27.1 package set: 1,567 tables, 2,708 pages, 1,748 codeunits; `Customer` with 183 fields
	and 132 methods. Objects nest under `Namespaces` from BC 26 on, so walk recursively. The
	project's *own* objects come from its built `.app`, which carries the same file.
	Passed over: the AL language server (licensed as part of the VS Code extension, built to be
	driven by an editor), tree-sitter (no AL grammar), a custom AL parser.
- [ ] **Decided 2026-09-10:** text search is `rg` wrapped as a native tool (ripgrep 15.1 is
	installed); the compiler is `alc.exe` from the installed AL extension (18.0.2732683), wrapped
	the same way.

### 3.3 Git and Azure DevOps

- [ ] Repository awareness (moved here from 3.2).
- [ ] Commits and history (moved here from 3.2).
- [ ] Branches.
- [ ] Diff and blame on demand.
- [ ] Pull requests: list, read, comment, create.
- [ ] Build status and pipelines.
- [ ] Work items: read, link to commits and PRs, update.
- [ ] Relate a work item or PR back to the project it belongs to (3.4).
- [ ] Eventually support full development workflow management.
- [ ] **Decided 2026-09-10:** Iris's first two MCP servers (2.3) — `mcp-server-git` (the reference
	server, via `uvx`) and Microsoft's official `@azure-devops/mcp` (via `npx`). Both pass through
	the confirm/audit spine, so a commit or a work-item update still gets a preview. Read-only
	surfaces first.
- [ ] **Decided 2026-09-10:** Azure DevOps authenticates through `az login` (Entra). Iris stores no
	secret; the CLI owns token refresh. `az` 2.87 is installed.
- [ ] Passed over: GitPython/pygit2 and hand-wrapping `az devops` — the servers already exist and
	are maintained by their owners.

### 3.4 Project and Task Awareness

- [ ] Define what a project *is*: a named record with folders, repos, an ADO area path, documents,
	and people attached.
- [ ] Track active projects.
- [ ] Associate conversations, files, code, decisions, and tasks per project.
- [ ] Switch projects explicitly, and infer the likely project from context (3.1).
- [ ] Scope memory per project (2.1).
- [ ] Remember current project state.
- [ ] Track unfinished work.
- [ ] Surface relevant prior decisions automatically.
- [ ] **Decided 2026-09-10:** where a project names an Azure DevOps area path, its tasks are a
	*view* over ADO work items through the ADO MCP server (3.3); otherwise Iris keeps a local list.
	One list per project, never two. The project record already has `paths.repository / workspace /
	documents` — empty on all three projects today — so linking is a data change, not a schema one.

---

## 4. Application Integration Layer

Goal: build adapters on top of existing automation APIs, not replacement applications.

Every adapter follows the same shape, so a new app is a plugin (2.3) rather than a redesign:

	detect (3.1) -> read -> propose -> confirm (10) -> apply -> verify -> undo

An adapter is only as trustworthy as its undo. Read-only ships first in every case.

### 4.1 Excel

- [ ] Detect active workbook — via 3.1, not a separate mechanism.
- [ ] Inspect sheets, formulas, tables, named ranges, and pivot tables.
- [ ] Read a closed workbook without opening Excel.
- [ ] Make controlled edits.
- [ ] Validate changes: recalculate, compare before and after, check for new errors.
- [ ] Add backup and undo protection (10).
- [ ] Handle the awkward cases explicitly: unsaved changes, protected sheets, a workbook the user
	is actively typing in, files locked by OneDrive.
- [ ] **Decided 2026-09-10:** the live workbook is reached through Excel COM via `pywin32` (already
	in the venv; verified against Excel 16). Attach to the running instance with `GetActiveObject`,
	never `Dispatch`, or Iris starts a second hidden Excel.
- [ ] **Decided 2026-09-10:** closed files go through `openpyxl`. The hand-rolled `zipfile` +
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
- [ ] **Decided 2026-09-10:** no custom extension. VS Code is already an MCP client, so once Iris is
	exposed as an MCP server (the 2.3 follow-on) VS Code and Copilot call Iris's tools directly,
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
- [ ] General web research.
- [ ] Technical information search.
- [ ] Documentation research.
- [ ] Image search.
- [ ] Video search.
- [ ] News and current information.
- [ ] Fetch and extract page content, not just search snippets.
- [ ] Compare multiple sources, and say when they disagree.
- [ ] Summarize findings, always with citations.
- [ ] Preserve useful findings in Iris memory when appropriate (2.1), with the source URL and
	the date retrieved.
- [ ] Cache results, and respect rate limits and site terms.
- [ ] Return structured results per 2.7, so the UI can show thumbnails, previews, and cards.
- [ ] **Decided 2026-09-10:** general search through a self-hosted SearXNG container — keyless,
	web/images/videos/news in one JSON API, and only the query leaves the machine, under SearXNG's
	identity rather than yours. Docker Desktop 29.5 is installed (the engine was not running at
	review time; starting it is a phase step). It is the one long-running service Iris depends on.
	Passed over: Tavily and similar (LLM-backed server-side — no other AI, 1), `ddgs` (scrapes
	DuckDuckGo; ToS-grey and brittle), Brave Search API (clean, but keyed and identity-bearing;
	the fallback if the container ever proves a burden).
- [ ] **Decided 2026-09-10:** fetch with `httpx`, extract with `trafilatura` (installed) — clean
	text, title, author, date. Playwright deferred until JavaScript-only pages actually block work;
	it is a ~300 MB browser download.
- [ ] The five keyless endpoints the fixed providers already call — DuckDuckGo instant answers,
	Google News RSS, stooq, wttr.in, worldtimeapi — stay as they are.

### 5.2 Document Intelligence

- [x] Ingest PDFs, Word, Excel, CSV, and text. -> `core/documents/extractors/`
- [x] Search across documents. -> `core/documents/search_service.py`
- [ ] Handle scanned documents and images of text (OCR).
- [ ] Handle PowerPoint, saved email (.msg/.eml), and Markdown.
- [ ] Preserve the structure worth having: headings, tables, and page numbers for citation.
- [ ] Extract and relate document content to projects (3.4).
- [ ] Cite the exact location — file, page, sheet, cell — when answering from a document.
- [ ] Keep the index current as files change (2.6).
- [ ] Later support editing and creating documents through proper integrations (4).
- [ ] **Decided 2026-09-10:** the xlsx and docx extractors move from hand-rolled `zipfile` +
	`ElementTree` to `openpyxl` and `python-docx` (both installed). Headers, footers, tables, and
	footnotes in Word; cached values, merged cells, and date serials in Excel — the cases the
	hand-rolled parsers do not see.
- [ ] **Decided 2026-09-10:** PDFs through `PyMuPDF` (installed), replacing `pypdf` — faster, real
	layout and tables, and it renders pages to images, which is the doorway to OCR.
- [ ] **Decided 2026-09-10:** scans through Windows' built-in OCR engine via `winocr` (installed,
	no separate installer), with `qwen2.5vl:7b` — already pulled — as the local vision fallback for
	what OCR cannot read. Tesseract passed over as one more installer to maintain.
- [ ] **Decided 2026-09-10:** document embeddings use the same `nomic-embed-text` as memory, in
	their own `sqlite-vec` table inside `documents.db`. One model, two stores, each backed up with
	its own database.
- [ ] Options still open: docling or unstructured for mixed corpora; python-pptx; extract-msg.

---

## 6. User Interface and Experience

Rendering only. *What* gets rendered is defined in 2.7.

- [ ] Replace the current plain UI with a modern experience.
- [ ] Design the UI as the central interface for all Iris capabilities.
- [ ] Render every result type from 2.7: rich response panels, image previews, video previews,
	syntax-highlighted code, diffs, file previews, tables, and search result cards.
- [ ] Stream responses as they generate, and let the user stop a running request.
- [ ] Show tool and action status clearly — what is running, what it touched, what it cost.
- [ ] Show pending confirmations and approvals prominently (10).
- [ ] Improve conversation and project organization (2.5, 3.4).
- [ ] Make it reachable instantly: tray icon, global hotkey, a small always-available input.
- [ ] Keyboard-first navigation and a command palette.
- [ ] Show sources, and let the user open the underlying file, page, or record in one click.
- [ ] Dark mode and readable defaults at the screen sizes actually used.
- [ ] **Decided 2026-09-10:** stay on PySide6, and render the result panels in an embedded
	`QWebEngineView` — `QtWebEngineWidgets` is already present in the installed PySide6 6.11.
	Streaming arrives with the `ollama` client change (2.4). A web front end on the FastAPI app was
	passed over: a second TypeScript codebase for a phone view that 11 does not ask for yet. The
	result contract (2.7) keeps that door open without touching core.

---

## 7. System, Network, and Infrastructure

### 7.1 System, PC, and Diagnostics

- [ ] Disk usage visibility (for example: "What is eating space on E:?").
- [ ] Drive health (SMART), including the drives holding Iris data and camera footage.
- [ ] Memory and RAM usage.
- [ ] Process visibility.
- [ ] CPU and GPU utilization, including VRAM — Iris shares the GPU with its own models.
- [ ] Temperatures and fan state.
- [ ] Windows Event Logs.
- [ ] Services status.
- [ ] Startup applications.
- [ ] Hardware information.
- [ ] Installed software and pending Windows updates.
- [ ] File-system tools.
- [ ] Prefer native Windows APIs/interfaces and proven open-source utilities.
- [ ] Expose all of this as declared tools (2.3), not ad-hoc shell calls.
- [ ] **Decided 2026-09-10:** `psutil` (installed) for CPU, RAM, disk, processes, and network
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

- [ ] Monitor conditions without repeated prompting.
- [ ] Define a watcher once — source, condition, threshold, channel — rather than coding each one.
- [ ] Watch PC health (7.1).
- [ ] Watch network problems (7.2).
- [ ] Watch development builds and pipelines (3.3).
- [ ] Watch cameras. Camera-specific behaviour is in 9.1; this is the delivery side.
- [ ] Watch files and folders (2.6).
- [ ] Watch services.
- [ ] Watch long-running jobs Iris itself started: renders, indexing, scans.
- [ ] Add trading-related monitoring later.
- [ ] Notify only on meaningful events: thresholds with hysteresis, deduplication, and suppression
	of a condition already reported.
- [ ] Respect quiet hours, and offer a "what did I miss" digest instead.
- [ ] **Decided 2026-09-10:** `APScheduler` (installed) is the scheduler — in-process, with its
	SQLite job store so watchers survive a restart. Iris had no scheduler at all; the only
	background machinery was cancel events and the UI's QThread. This also answers PhaseStatus's
	open question of what triggers TEST and MEASURE without a person. Passed over: Windows Task
	Scheduler (one process per job, no shared state), a hand-rolled asyncio loop.
- [ ] **Decided 2026-09-10:** first delivery channel is Windows toast via `windows-toasts`
	(installed, native WinRT). Phone push (ntfy, Pushover), email, and Teams are not built until
	an away-from-desk need is real.

### 8.3 Workflow Automation

- [ ] Chain tools into workflows.
- [ ] Support patterns like:
	detect event -> gather info -> analyze -> store result -> notify.
- [ ] Triggers: manual, scheduled, and event-driven.
- [ ] Handle failure explicitly: retry, timeout, partial completion, and what to do about each.
- [ ] Support a human approval step mid-workflow (10).
- [ ] Dry-run a workflow before enabling it.
- [ ] Store workflows as data — versioned, editable, and disableable.
- [ ] Add reusable workflows over time.
- [ ] Keep this built on the plugin/tool architecture (2.3), not as a separate automation stack.
- [ ] **Decided 2026-09-10:** in-house on the tool layer, with `APScheduler` (8.2) supplying the
	scheduled and interval triggers and event triggers coming from watchers and MQTT (9.1).
	n8n and Node-RED passed over; a visual editor has not been asked for.

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

- [ ] Enforce read vs write vs execute boundaries.
- [ ] Scope permissions to targets, not just verbs: allowlisted paths, repos, hosts, devices.
- [ ] Require confirmation for dangerous actions.
- [ ] Show exactly what will change before confirming — diff, file list, target device.
- [ ] Backup before destructive file changes.
- [ ] Provide undo/rollback where possible, and say plainly when an action is irreversible.
- [ ] Log all changes Iris makes (2.8). Today only memory changes and actions are logged.
- [ ] Show diffs.
- [ ] Keep credentials and secrets separate from model reasoning: never in `config.json`, never in
	a prompt, never in a log.
- [ ] Rate-limit and cap physical-world and outbound actions: email, messages, devices.
- [ ] Provide a global stop that halts running tools, workflows, and watchers at once.
- [ ] Nothing leaves the machine by default — decided 2026-09-10. There is no cloud AI (1, 2.4);
	any non-AI outbound call (search, Graph, notifications) names what it sends, and memory,
	documents, and camera content are never part of it.
- [ ] Increase rigor as Iris shifts from advisory to action-taking behavior.
- [ ] **Decided 2026-09-10:** secrets live in Windows Credential Manager through `keyring`
	(installed; it selects the Windows backend itself, no service). First tenants: the UniFi API
	key, the Blue Iris login, the MQTT credentials, later Graph tokens. Passed over: raw `win32cred`
	(more code for the same store), an encrypted file, `.env`.

---

## 11. Runtime and Operations

How Iris runs day to day. None of this was in the original document, but every item is already a
real thing in the repo: `Launch-Iris.cmd`, `deploy_phase4.ps1`, `package_release.ps1`, `serve.py`,
and the `Data/` tree.

- [ ] **Decided 2026-09-10:** a headless Windows service hosts the parts that must run when no
	window is open — APScheduler watchers, the MQTT listener, the HTTP surface — and the Qt window
	is a client of it. `win32serviceutil` from the pinned `pywin32`; no new dependency. Today Iris
	is a console launcher (`Launch-Iris.cmd`) with no startup entry at all.
- [ ] Start with Windows, survive a reboot, and recover from a crash.
- [ ] **Decided and done 2026-09-10:** one `requirements.txt` at the root for dev (`.venv`) and
	release (`Runtime\venv`). `core/requirements.txt` and `ui/requirements.txt` are removed and
	`deploy_phase4.ps1` now installs from the root file — before this, the release build read
	`core/requirements.txt`, which listed only `pypdf`, and would have missed every dependency
	decided today. `pywin32` is pinned explicitly rather than left transitive, since Excel COM,
	the service host, and 3.1 all depend on it.
- [ ] One documented way to launch dev, one for release.
- [ ] Config: one schema, validated on load, clear errors, no secrets (10).
- [ ] Document the data directory layout, and add migrations for the SQLite databases.
- [ ] Back up `Data/` — memory, sessions, audit, index — and test a restore.
- [ ] SQLite durability for `knowledge.db` and `conversations.db` — decided 2026-09-10, stdlib only:
	switch `journal_mode` from DELETE to WAL (Data/ is on a local, unsynced drive, so WAL is safe);
	run `PRAGMA quick_check` on startup and refuse to write to a database that fails it;
	take a consistent online copy with `sqlite3.Connection.backup()` on a schedule and before every
	schema migration, keeping a bounded number of copies. The hand-rolled `SCHEMA_VERSION` ladder
	stays; Alembic/yoyo would be more machinery than problem.
- [ ] An update mechanism that does not lose data or config.
- [ ] Resource limits: do not hold the GPU or thrash the disk while the user is working.
- [ ] Health check: what is up, which model is loaded, what is indexed, what is broken.
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
- [ ] A latency budget per request class, and a warning when it is exceeded.
- [ ] Record failures in normal use — wrong tool, wrong answer, bad context — and feed them into
	2.2 instead of losing them.
- [ ] Regression check before swapping a model or changing a routing rule (2.4).

---

## Appendix A. Open Questions

Collected from the sections above. Each one changes the shape of the work that follows it.

1. ~~**Embeddings** (2.1, 5.2)~~ — closed 2026-09-10: `nomic-embed-text` + `sqlite-vec` for both;
	memory vectors in `knowledge.db`, document vectors in `documents.db`.
2. **MCP** (2.3) — decided: a native registry with `mcp` as a client inside it, and native
	tool-calling for selection. Still open: when Iris becomes an MCP server itself.
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
10. **Trading** (8.2) — Iris as the decision layer for the bot is deferred. When it returns, it
	enters through 2.1 and 2.2 as an application, per `PhaseDesign.md`.
