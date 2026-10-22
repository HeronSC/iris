# Iris – Phase A: Natural Conversation

Iris should understand Henry the way any modern assistant does. Today she doesn't. Ask for the weather as "whats it like out today?", "is it gonna rain?" or "do I need an jacket today?" and she gets confused, because all three fail the hand-written patterns that decide what she does before the model ever sees the message. Every new capability has also meant writing code, including small things like weather that the model should handle by itself.

Phase A fixes this **locally, on qwen3:8b, with no new dependencies and nothing leaving the machine**. It is the first step of the larger plan: Phase B makes the LLM layer provider-neutral, and Phase C adds Claude as the brain. Phase A comes first for two reasons:

1. Claude would hit the same pattern gates and read out the same raw tool output. Those have to go no matter which model answers.
2. It shows how much of "not smart enough" was wiring and how much was qwen. Phase C is then judged on what is actually left.

---

## 1. What is wrong today

### 1.1 Patterns decide before the model does

After the slash commands, a typed or spoken message passes through eight layers that interpret wording. In this order, any of them can claim it before the model is called (`service.py:829-848`, then the graph).

| # | Layer | Where | What it does |
|---|---|---|---|
| 1 | Topic-memory edits | `service.py:1277` `_should_route_to_topic_memory` | Substrings such as "remove ", "exclude " or "put back" send the message to topic-memory editing. |
| 2 | File intents | `service.py:1309` `_should_prefer_coordinator_routing`, `request_pipeline` | The request pipeline's intent parse sends file reads and searches down the coordinator file path. |
| 3 | Intent router | `intent_router.py`, `service.py:835` | Substring triggers (store/save/remember/use plus confirm/cancel; index/scan plus a drive path), and an exact-match example store that replays the arguments of past tool calls. |
| 4 | Action phrases | `action_commands.py:130` `handle_natural_language` | yes/no for a pending confirmation, plus regexes for "open 3", "show … 2", and "launch/open anything" that launches whatever follows. |
| 5 | File search | `search_commands.py:59` `handle_natural_language` | Anything that looks like a search is answered with a local file search. |
| 6 | Graph classify | `graph.py:209`, `request_kinds.py` | File intents and `/help` go to file ops. `is_small_talk` sends greetings to a no-tools answer. `is_code_request` matches words like class, query, bug, script, java, method or endpoint and sends the message to a no-tools code answer. |
| 7 | Capability router | `general_knowledge_router.py` `route()`, `graph.py:240` | Regex and substring tests for weather, news, stocks, calculator, conversion, time and lookup. A match is answered without the model. |
| 8 | Local-request check | `general_knowledge_router.py:415` `_looks_like_local_request` | Decides whether the capability router may run at all. |

Misses (natural phrasing that no pattern catches):

- "whats it like out today?" — the weather pattern needs "what's it like **outside**".
- "is it gonna rain?" — the pattern needs "**will** it rain".
- "do I need an jacket today?" — the pattern allows only "a".
- "what time is it?" — time needs "time in" or "current time", so this goes to the DuckDuckGo lookup instead.

False hits (messages a pattern claims when it shouldn't):

- "is the trading bot running?" — the word "trading" triggers a stock quote.
- A one-word reply like "why" or "stop" — any word of five letters or fewer is treated as a stock ticker.
- "what time is my yoga class?" and "query my ADO work items" — "class" and "query" mark these as code requests, so no tools are offered.
- "explain E:\AI\iris\core\documents\scanner.py" — "scan" inside "scanner", plus a path, triggers a document-root scan.
- "any news on my order?" — the word "news" triggers headlines.
- "open the garage door" — layer 4 tries to launch an application called "the garage door".

### 1.2 Too many tools for an 8B model

`_tool_specs` (`graph.py:385`) sends every enabled tool on every turn:

- about 60 actions
- the 5 capability providers
- all git MCP tools
- the entire Azure DevOps MCP set

That is an estimated 130–160 tool definitions, and step A1 measures the real number. At that size an 8B model picks badly, and the definitions take up most of the context window.

### 1.3 Broken tool schemas

Capability schemas use made-up types like `{"ticker": "string"}` (`general_knowledge_router.py:472, 1248, 1344, 1620, 1705`). `capability_tool_specs` (`graph.py:70`) only adds `type: object`, so the model sees these tools with no usable parameters.

### 1.4 Tool output becomes the answer

`_act` (`graph.py:308-356`) joins the tools' `message` strings and returns them as the reply. The step that hands results back to the model only runs when a tool sets `needs_model`, and no tool does. On top of that, `complete_turn` (`coordinator.py:355`) replaces the text with facts rendered from the capability. The result is what Henry hears: "Current weather in Anderson, SC: Partly cloudy, 72.5F."

### 1.5 Missing basics in the prompt

- No current date or time anywhere, so "today", "tomorrow" and "this weekend" mean nothing to the model.
- The home location (Anderson, South Carolina, from `profile.json`) reaches only the weather provider's own defaults, not the model.
- The weather location regex is anchored to the end of the sentence. "weather in Boston?" falls back to the default location because of the "?", and "weather for the weekend" reads "the weekend" as a place.

### 1.6 Extra model calls on every turn

After the answer, `_respond_with_coordinator` (`service.py:1411, 1427`) runs `suggest_follow_up_actions` and `summarize_for_chat` one after the other. Both happen before the reply is shown or spoken. A plain voice turn can cost three qwen calls.

### 1.7 Voice edge case

If the model streams any text before it calls a tool, `ui/main.py:625-644` treats speech as already started. The tool's answer is then never spoken.

---

## 2. Target behaviour

One turn should look like this:

1. **No pre-model routing (Henry, 2026-10-19).** Every message goes straight to the model, and the model decides what is wanted. No code interprets wording before the model sees it. Only controls stay in front of the model (decision 2):
   - slash commands, which are exact syntax Henry typed on purpose
   - yes/no while a confirmation is pending
   - stop and cancel
   - the wake word
2. The model sees Iris's persona, the date and time, the home location, recent conversation, and a **small core set of tools** with real descriptions, plus any tool group it opens itself.
3. The model either answers directly or calls tools.
4. Tool results go **back to the model** as tool messages, and the model writes the reply.
5. The reply follows Iris's speech rules and is shown and spoken as written. Full data such as forecast tables still goes to the details pane through the existing result contract (`to_detail`). Only the spoken and chat text changes.
6. Follow-up suggestions, if any, come after the reply and never delay it.

Rule of thumb for new capabilities after this phase: **code is written only for things the model cannot reach** — the PC, files, the home, Henry's accounts, Iris's memory, and big analyses like the market tool. Public information goes through web search and web research, which already exist.

---

## 3. Work items

Do them in this order, one commit each.

### A1. Baseline phrase set

Build on the existing `/eval` harness (`core/assistant/eval_command.py`, `Data\Evaluationequests.jsonl`, checklist 12) rather than writing a new runner. Today `/eval` checks only the classifiers, with no model call, and A4 removes those classifiers. So:

- Add a `tool=<name>` expectation (`tool=none` for a direct answer) beside `kind`, `intent` and `memory`.
- Add `/eval live`, which drives each saved request through the agent graph against live qwen3:8b and records:
  - the route taken (pattern, capability, model, tools)
  - the tool and arguments chosen
  - the reply text
  - the number of model calls
  - the latency
  - the size of the tool list sent
- Load the starter phrase set from §6 into `requests.jsonl` with its expected tools.

Run `/eval live` before any change and keep the run in `runs.jsonl`. This baseline is the measure for every later step, and for Phase C.

`/eval live` needs Ollama running and is run by hand. The existing `kind` and `intent` expectations are retired once A4 lands.

### A2. Fix the tool definitions

- Rewrite the capability `request_schema`s as real JSON Schema: types, enums for `range_name` and similar fields, `required`, and a description for every field.
- Rewrite tool descriptions in Henry's terms, written for the model. Example for weather: "Current conditions and forecast. Use for any question about weather, rain, temperature, wind, or what to wear or bring outside. Location defaults to home."
- Check every action and MCP tool description in the same pass. Hide tools the model should never call on its own (`expose_to_model=False`).

### A3. Small tool set per turn

Target: about 15–20 tools per call.

- **Always on:** weather, time, calculator and conversion, web search, web research, fetch page, launch application, open URL, memory recall.
- **Everything else in groups:** files, git, Azure DevOps, config, market, screen, documents.
- **The model loads groups itself.** An always-on `open_toolbox(name)` tool lists each group with a one-line description. When the model needs a group, it calls `open_toolbox`, and the next call in the same turn carries that group's tools. This costs one extra model call, but only on turns that need a group.

Choosing groups with embedding similarity was considered and dropped: it would be code deciding before the model, which section 2 rules out.

The Azure DevOps server also gets an allowlist in `mcp_servers` so only the read tools Henry uses are registered.

### A4. Remove all pre-model routing

After the slash commands, `process_input` (`service.py:829-848`) goes straight to the agent. The graph starts at `plan`. Layer by layer, from §1.1:

1. **Topic-memory edits:** removed. Editing topic memory becomes a tool.
2. **File intents:** removed. Reading, counting and finding files are already actions (`file_tools`), so they live in the files group. The graph's `classify` and `file_ops` nodes go.
3. **Intent router:** removed from the natural-language path. Phrase teaching, clarification and correction stay reachable through their slash commands. The example store stops replaying past arguments.
4. **Action phrases:** the open/show/launch regexes are removed, and launching becomes the always-on `launch_application` tool. yes/no for a pending confirmation stays (decision 2).
5. **File search:** removed. Search becomes a tool in the files group.
6. **Graph classify:** removed:
   - `is_small_talk` goes. A greeting costs one model call with tools available, which is fine.
   - `is_code_request` goes. Code questions go through the normal path (decision 4).
   - `/help` stays as a slash command.
7. **Capability router:** `route()` is no longer called. The providers stay, as tools only. Delete:
   - the `can_handle` and `can_handle_follow_up` matchers
   - the `_last_provider_name` follow-up logic. Follow-ups like "what about tomorrow?" work from conversation history, which the model already gets.
   - `_extract_location`, `_extract_ticker` and the other text parsers whose job the schema now does
8. **Local-request check:** deleted along with the router.

Trade-off: requests that patterns answered instantly, like "open Outlook", now cost one qwen call, about a second or two on the 4060 Ti.

### A5. Results go back through the model

- `_act` always returns to `_plan` with the results. The exceptions are a pending confirmation and a run that has reached `max_iterations`.
- Tool results are sent as an assistant tool-call message followed by the matching tool messages, not as orphan tool messages (`graph.py:249`). Ollama accepts this, and it is the shape Phase B and Claude need.
- `complete_turn` no longer overwrites the model's text with capability facts. Capability details still go to the details pane.
- File listings follow the same rule. The full list goes to the details pane, and the model says what it found.
- Speech: text streamed before a tool call is a preface ("Let me check."). It is spoken, and the final answer after the tool call is spoken too (fixes 1.7).

### A6. Iris's persona and speech rules

Replace the opening of `_build_system_prompt` (`coordinator.py:835`) with one persona block shared by every route, including the refusal route, which today swaps in a different system prompt. It has three parts:

- **Who Iris is:** the persona text below (decision 3). It lives in its own text file, `core/persona.md`, which Iris reads when she builds the prompt, so Henry can refine it without touching code. `assistant_name` stays in config.

  ```text
  You are Iris, Henry's personal assistant. You run on his PC at home in Anderson, South Carolina, and help with whatever he's doing: work, the house, the cameras, the market, or just talking.

  Talk like a sharp, friendly person who knows him well, not like a help desk. Warm and relaxed, with a little dry humor when it fits. Never gushing, and never apologize more than once.

  Call him Henry. Be casual by default, and crisp and to the point when he's clearly working.

  Have opinions. If he asks what you think, say it and give the reason in a sentence. If something looks like a bad idea, tell him plainly. Never be a "Yes man", don't tell him what he wants to hear, tell him the truth.

  Be honest about what you don't know or couldn't do. Never pretend a tool worked or guess at a fact.

  You're an AI and don't pretend otherwise, but you don't bring it up unless it matters.
  ```

- **Speech rules, voice turns** (extends `VOICE_TURN_BLOCK`):
  - Answer first, in one or two sentences.
  - Never read raw data. Round numbers ("about eighty degrees", "around a quarter past three").
  - No lists, symbols, units written as abbreviations, or URLs.
  - Use contractions and casual connectors, the way a person talks.
  - Lead with the headline and offer more instead of giving it all.
  - Mention only what is useful or unusual: rain later, a big drop, a warning.
- **Text turns:** the same voice, but short lists and links are allowed when they actually help.

Add to every prompt, as the last block:

- the current date, time and day of the week
- the home location from the profile

### A7. One model call per turn

- Remove `summarize_for_chat` from the normal path. The model's reply is already the conversational text, and `speech_text` still cleans it for voice.
- `suggest_follow_up_actions` runs after the reply is emitted, in the background, and is off for voice turns (decision 5).
- Target: one qwen call for a turn with no tools, two for a turn with one round of tools.

---

## 4. Out of scope

| Item | Phase |
|---|---|
| Provider-neutral routes, real message history, Ollama code confined to its client | B |
| Claude adapter, prompt caching, Claude's web tools, outbound-data policy, cost cap | C |
| Venice | D, only if C shows a need |
| Email, Teams, EDGAR, E*TRADE read access | E |
| Emotion tags, lip sync, cloned voice | F |

---

## 5. Decisions

1. **Decided 2026-10-19: no pre-model routing.** Everything goes to the model. Tool groups are loaded by the model through `open_toolbox`, not chosen by embeddings.
2. **Decided 2026-10-19: four controls stay in front of the model.** Slash commands, yes/no while a confirmation is pending, stop and cancel, and the wake word. None of them interprets wording, and approving a destructive action should not depend on how the model reads "sure, why not".
3. **Decided 2026-10-19: persona.** The text in A6, written by Henry as a starting point that he may refine later. It is kept in `core/persona.md`.
4. **Decided 2026-10-19: code requests.** No more routing to `qwen2.5-coder:7b`. qwen3 answers code questions with tools available, and Claude takes code in Phase C.
5. **Decided 2026-10-19: follow-up suggestions.** They run after the reply is emitted and are off for voice turns.
6. **Decided 2026-10-19: old matcher code.** It is deleted, not kept around unused.

---

## 6. Starter phrase set

Each phrase is listed with its expected tool. "None" means answer directly.

**Weather** (expected: weather)
- whats it like out today?
- is it gonna rain?
- do I need an jacket today?
- should I bring an umbrella this afternoon?
- how cold is it supposed to get tonight?
- what's the weekend looking like?
- is it nice enough to cut the grass tomorrow?
- what's the weather in Boston?
- and Thursday? (follow-up after a weather question)

**Time and date** (expected: none, from the prompt)
- what time is it?
- what's today's date?
- how many days until Christmas?
- what time is it in Tokyo? (expected: time)

**Math and conversion** (expected: none, or calculator)
- what's 18 percent of 64.50?
- how many ounces in a liter?
- if I drive 340 miles at 28 mpg how much gas is that?

**Public information** (expected: web search or web research)
- who won the game last night?
- what's going on with the stock market today?
- how do I get rid of moles in my yard?
- any news about the hurricane?
- what's Apple trading at? (expected: stock quote, or web search)

**PC and home** (expected: the matching action)
- open Outlook
- pull up the camera on the driveway
- how much space is left on the NAS?
- is the trading bot running?

**Work and code** (expected: the matching group)
- query my ADO work items for this sprint
- is there a bug filed for the login page?
- what changed in the iris repo today?
- explain E:\AI\iris\core\documents\scanner.py (expected: read file, not scan)

**Memory** (expected: memory recall)
- what laptop did we decide on?
- what did I say about the yoga class?

**Conversation** (expected: none)
- hey Iris
- thanks, that's all
- why? (follow-up to any answer)
- tell me something interesting
- what time is my yoga class? (expected: memory recall or a "don't know"; never code, never a ticker)

---

## 7. Done when

- **Routing:** every phrase in §6 reaches the right tool or no tool. Report the hit rate per category on qwen3:8b, before and after.
- **Speech:** no spoken reply contains raw fields such as "72.5F" or "Partly cloudy," lists, URLs or symbols.
- **Model calls:** one per turn with no tools, two per turn with one round of tools.
- **Latency:** a voice turn is no slower than the A1 baseline. With fewer calls and a smaller tool list it should be faster.
- **Tools per call:** the tool list stays within about 20.
- **Tests:** the tests that assert the old routing are rewritten or retired, file by file:

  | File | Tests |
  |---|---|
  | `core/test_phase4_8_slice2.py` | 44 |
  | `core/test_phase4.py` | 31 |
  | `core/tests/test_general_knowledge_router.py` | 26 |
  | `core/tests/test_application_prompt_and_cancel.py` | 26 |
  | `core/tests/test_knowledge_provider.py` | 18 |
  | `core/tests/test_refusal_reroute.py` | 18 |
  | `core/tests/test_agent_graph.py` | 12 |
  | `core/test_intent_router.py` | 6 |
  | `core/tests/test_streaming.py` | 5 |
  | `core/tests/test_routing_fixes.py` | 4 |

  Targeted tests per change; the full suite runs only when Henry asks.

The rerun of the phrase set is also the input to Phase C. Whatever qwen still gets wrong, now that the wiring is fixed, is what Claude is for.
