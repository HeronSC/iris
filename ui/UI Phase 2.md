# Iris UI — Phase 2 Development Specification

## Objective

Phase 2 will convert the current Iris UI from a basic chat client into a two-pane assistant interface.

The two panes must serve different purposes:

* The **Chat pane** is Iris speaking directly to the user.
* The **Details pane** contains the structured information that supports the conversation.

The distinction is not “short response versus long response.”

It is:

* Conversation
* Supporting evidence, metadata, records, and actions

The UI must be built around that separation.

---

# 1. Core Design Principle

Every Iris response may contain two complementary layers.

## Chat layer

The Chat pane should contain a natural, conversational response from Iris.

It should:

* Explain what Iris found, did, or needs.
* Mention the most important results by name.
* Provide enough context for the user to understand the outcome.
* Ask a natural follow-up question when appropriate.
* Support a future Iris personality.
* Remain useful even if the Details pane is hidden or collapsed.

Example:

> I found three likely matches: a.pdf, b.pdf, and y.pdf. They are stored in two different folders. Would you like me to open one, copy a path, or compare their contents?

The Chat pane must not normally reduce useful results to messages such as:

> Found 3 files.

That is too mechanical and does not provide enough conversational context.

## Details layer

The Details pane should contain the structured facts behind the conversational response.

For file search results, this may include:

* File name
* Full path
* Parent directory
* File type
* File size
* Created date
* Modified date
* Match score
* Match reason
* Matching content excerpt
* Indexing status
* Available actions

The Details pane should not merely repeat the Chat response in a longer paragraph.

It should present the actual evidence, data, metadata, and controls that support what Iris said.

---

# 2. Phase 2 Scope

Phase 2 includes:

1. A two-pane UI layout
2. A stable structured response contract
3. Conversational Chat output
4. Structured Details output
5. Multiple detail renderers
6. Confirmation controls
7. Better request-state handling
8. Copy and action controls
9. Persistent UI layout preferences
10. Graceful error handling

Phase 2 does not include:

* Voice input
* Speech output
* Avatar support
* Conversation history across application restarts
* Full settings screen
* Drag-and-drop file handling
* File preview rendering
* System tray support
* Background listening
* Token-by-token streaming
* Complex animation
* Final personality configuration

The architecture should allow these later, but they are not part of Phase 2.

---

# 3. Required Response Contract

Every call to the Iris engine must return the same top-level response model.

This includes:

* Normal conversation
* Slash commands
* File searches
* Indexing commands
* Confirmation requests
* Confirmed actions
* Cancelled actions
* Errors

No response path should print directly to the console as its primary output.

No response path should return an unrelated primitive type such as a string, tuple, or Boolean.

Use one consistent response structure.

Recommended shape:

```json
{
  "status": "complete",
  "conversation": {
    "message": "I found three likely matches: a.pdf, b.pdf, and y.pdf. Would you like me to open one or copy a path?",
    "suggested_actions": [
      {
        "id": "open_file",
        "label": "Open a file"
      },
      {
        "id": "copy_path",
        "label": "Copy a path"
      }
    ]
  },
  "details": {
    "type": "file_results",
    "title": "Matching files",
    "items": []
  },
  "confirmation": null,
  "error": null,
  "metadata": {}
}
```

Recommended top-level fields:

```text
status
conversation
details
confirmation
error
metadata
```

Not every response needs all fields populated, but every field should exist or have a predictable default.

---

# 4. Response Statuses

Use a small, explicit status set.

Recommended statuses:

```text
complete
awaiting_confirmation
cancelled
error
```

The UI may internally display transient states such as:

```text
sending
thinking
indexing
opening
```

However, those temporary UI states do not need to be permanent engine response statuses unless the engine genuinely emits progress events.

For Phase 2, the UI may infer the temporary busy state while waiting for the engine response.

---

# 5. Conversation Model

Recommended structure:

```json
{
  "message": "I found three likely files.",
  "suggested_actions": [
    {
      "id": "open_file",
      "label": "Open a file",
      "payload": {}
    }
  ]
}
```

Required behavior:

* `message` is the natural-language response shown in Chat.
* The message should be written by the engine, not constructed by the UI.
* The UI must not rewrite or summarize the engine message.
* The UI must not generate personality language itself.
* Suggested actions may be displayed as buttons or action chips.
* Suggested actions should be optional.

The UI should remain a renderer and interaction layer.

The engine remains responsible for what Iris says.

This is important because personality will be added later at the assistant level.

---

# 6. Details Model

Recommended structure:

```json
{
  "type": "file_results",
  "title": "Matching files",
  "summary": "Three files matched the search.",
  "items": [],
  "actions": []
}
```

Recommended fields:

```text
type
title
summary
items
actions
metadata
```

The `type` field determines which Details renderer the UI uses.

The UI must support unknown detail types gracefully.

If the UI receives an unsupported type, it should render a generic fallback instead of failing.

---

# 7. Required Detail Types

Phase 2 should support at least the following detail types:

```text
text
markdown
file_results
file_list
search_results
command_output
index_summary
error
confirmation
```

## text

Plain text content.

## markdown

Formatted text such as headings, lists, emphasis, and code blocks.

## file_results

Structured file search results with metadata and actions.

## file_list

A direct list of files that may not have search scoring or excerpts.

## search_results

Generic structured search results that are not necessarily files.

## command_output

Output from slash commands such as `/help`.

## index_summary

Indexing totals, skipped files, failures, and timing information.

## error

Detailed error information that supports the shorter Chat explanation.

## confirmation

Structured information about an action awaiting approval.

---

# 8. File Result Structure

Recommended file result model:

```json
{
  "id": "result-1",
  "name": "a.pdf",
  "path": "E:\\Documents\\a.pdf",
  "directory": "E:\\Documents",
  "extension": ".pdf",
  "size_bytes": 184320,
  "created_at": "2026-07-28T14:30:00",
  "modified_at": "2026-07-31T09:15:00",
  "match_score": 0.91,
  "match_reason": "Filename and content matched",
  "excerpt": "Relevant matching text from the document...",
  "indexed": true,
  "actions": [
    {
      "id": "open",
      "label": "Open",
      "payload": {
        "path": "E:\\Documents\\a.pdf"
      }
    },
    {
      "id": "copy_path",
      "label": "Copy path",
      "payload": {
        "path": "E:\\Documents\\a.pdf"
      }
    },
    {
      "id": "open_folder",
      "label": "Open folder",
      "payload": {
        "path": "E:\\Documents"
      }
    }
  ]
}
```

The UI should format file sizes into readable values such as:

```text
180 KB
2.4 MB
1.1 GB
```

The original byte value should remain available in the data model.

---

# 9. Two-Pane Layout

The main window should contain:

## Left or primary pane

Chat conversation.

## Right or secondary pane

Details supporting the currently selected or most recent response.

Recommended layout behavior:

* Chat should remain the primary visual area.
* Details should be resizable.
* The divider position should be remembered between sessions.
* The Details pane should be collapsible.
* Collapsing Details must not remove or damage the data.
* The UI should remain usable at smaller window sizes.
* Each pane should scroll independently.

The Details pane should display the latest relevant detail response by default.

Do not automatically duplicate the entire chat history in the Details pane.

---

# 10. Chat Pane Requirements

The Chat pane should support:

* User messages
* Iris messages
* Error messages
* Confirmation prompts
* Multiline responses
* Markdown rendering
* Code blocks
* Auto-scroll
* Copy response
* Clear visual separation between user and Iris

Input behavior:

* Enter sends
* Shift+Enter inserts a new line
* Sending is disabled while a request is being processed, unless the architecture safely supports parallel requests
* Duplicate sends must be prevented
* Empty messages must not be submitted

The Chat pane should not display raw JSON.

---

# 11. Details Pane Requirements

The Details pane should support:

* Structured cards or rows
* Scrollable result lists
* Expandable metadata
* Copy controls
* Open controls
* Open-folder controls
* Result selection
* Error details
* Command output
* Indexing summaries
* Generic fallback rendering

For file result lists, each result should visibly show at least:

* File name
* Directory
* File size
* Modified date
* Match reason, when available

Additional metadata may be collapsed by default.

Do not overload the initial file row with every available field.

The full path should be available, but it does not need to dominate the layout.

---

# 12. Confirmation UI

Phase 2 should support visible confirmation controls.

When the engine returns:

```text
awaiting_confirmation
```

the Chat pane should show the natural-language explanation.

The UI should also show controls such as:

```text
Approve
Cancel
```

The Details pane may show:

* Action name
* Target file or object
* Full path
* Risk or impact
* Parameters
* Reason confirmation is required

Typing `/confirm` should remain supported for compatibility.

Typing a cancellation command should also remain supported if already available.

The buttons should call the same underlying confirmation logic as the command path.

Do not create a second confirmation implementation exclusively for the UI.

---

# 13. Suggested Actions

Suggested actions are not the same as security confirmations.

Examples:

```text
Open file
Copy path
Open folder
Compare files
Summarize file
Show more results
```

Suggested actions may be presented as buttons below the Iris message or inside the Details pane.

The action payload must contain enough information for the engine or application layer to execute the action safely.

Do not make the UI infer file paths or action parameters from displayed text.

---

# 14. Error Handling

The UI must not crash because of:

* Engine exceptions
* Unknown response types
* Missing optional metadata
* Invalid result entries
* Rendering failures
* File actions that fail
* Empty responses

Recommended error response:

```json
{
  "status": "error",
  "conversation": {
    "message": "I could not open a.pdf because the file is no longer available."
  },
  "details": {
    "type": "error",
    "title": "File open failed",
    "items": [],
    "metadata": {
      "path": "E:\\Documents\\a.pdf",
      "exception_type": "FileNotFoundError"
    }
  },
  "confirmation": null,
  "error": {
    "code": "file_not_found",
    "message": "The requested file does not exist."
  },
  "metadata": {}
}
```

The Chat pane should show the user-friendly explanation.

The Details pane may show technical information useful for diagnosis.

Do not expose a full stack trace in the Chat pane.

A stack trace may be written to logs.

---

# 15. Logging

Add basic logging for:

* Engine request start
* Engine request completion
* Response status
* Rendering failures
* Button action failures
* Confirmation failures
* Unexpected response types

Logs should be useful for debugging without flooding the file.

Do not log sensitive content unnecessarily.

Do not log entire document contents or full conversation payloads by default.

---

# 16. Persistence

Phase 2 should remember basic UI layout state:

* Window width
* Window height
* Window position, if practical
* Details pane width
* Details pane collapsed or expanded state

Do not add full conversation persistence in this phase.

Do not add a database solely for UI preferences.

Use a simple local settings mechanism appropriate to the existing application architecture.

---

# 17. Personality Preparation

Phase 2 does not define the final Iris personality.

However, the architecture must prepare for it.

Rules:

* Do not hard-code robotic response phrases in the UI.
* Do not let the UI generate conversational summaries.
* Do not make file result renderers produce Iris dialogue.
* Do not embed personality choices in individual UI components.
* Keep conversational wording in the engine response.

Later phases may add personality controls such as:

* Warmth
* Directness
* Humor
* Formality
* Verbosity
* Proactive suggestions
* Conversational style

Phase 2 only needs to ensure the Chat pane can naturally display those responses later.

---

# 18. Implementation Order

## Phase 2.1 — Structured Response Contract

Refactor the engine boundary first.

Every response from `IrisApplication.process_message()` or its equivalent must return the same response object.

Update:

* Normal messages
* Slash commands
* Help
* Indexing
* File searches
* Confirmation requests
* Confirmation completion
* Cancellation
* Errors

Do not begin advanced UI rendering until this contract is stable.

## Phase 2.2 — Two-Pane Shell

Create the Chat and Details layout.

Initially, both panes may render simple text from the structured response.

Confirm:

* Resizing works
* Scrolling works
* Divider works
* Details can collapse
* State persists

## Phase 2.3 — Detail Renderers

Add renderer selection based on `details.type`.

Implement the required detail types.

Use a generic fallback renderer for unknown types.

## Phase 2.4 — File Result Renderer

Add structured file cards or rows.

Support:

* Open
* Copy path
* Open folder
* Metadata expansion

## Phase 2.5 — Confirmation Controls

Add Approve and Cancel buttons.

Route them through the existing confirmation system.

Preserve command compatibility.

## Phase 2.6 — Resilience and Polish

Add:

* Busy-state handling
* Duplicate-send prevention
* Graceful error rendering
* Logging
* Copy response controls
* Persistent pane sizing
* Unknown-response fallback

---

# 19. Architectural Rules

Follow these rules throughout Phase 2:

1. Preserve the separation between the UI and Iris engine.
2. Do not place assistant reasoning or personality logic in the UI.
3. Do not let UI components directly perform unsafe actions without passing through the application or engine action layer.
4. Use typed models where practical.
5. Avoid loosely structured nested dictionaries spreading throughout the UI.
6. Keep response parsing centralized.
7. Keep renderer selection centralized.
8. Avoid broad rewrites unrelated to Phase 2.
9. Preserve existing Phase 1 behavior unless Phase 2 intentionally replaces it.
10. Do not add unrelated features.

---

# 20. Recommended Internal Models

Use typed models similar to:

```python
class IrisResponse:
    status: ResponseStatus
    conversation: ConversationContent
    details: DetailContent | None
    confirmation: ConfirmationContent | None
    error: ErrorContent | None
    metadata: dict
```

```python
class ConversationContent:
    message: str
    suggested_actions: list[Action]
```

```python
class DetailContent:
    type: str
    title: str | None
    summary: str | None
    items: list
    actions: list[Action]
    metadata: dict
```

```python
class Action:
    id: str
    label: str
    payload: dict
```

Exact implementation may vary based on the current codebase.

The important requirement is a predictable, validated structure.

---

# 21. Acceptance Criteria

Phase 2 is complete when all of the following are true:

* Every engine request returns the same structured response model.
* Chat and Details content are clearly separated.
* The Chat response remains conversational and useful on its own.
* File results mention important filenames in Chat.
* Full file metadata appears in Details.
* `/help` output renders cleanly in Details.
* Indexing output renders as a structured summary.
* Confirmations can be approved or cancelled using visible controls.
* Existing confirmation commands still work.
* Errors do not crash or freeze the client.
* Unknown detail types render using a fallback.
* Duplicate sends are prevented.
* The Details pane can be resized and collapsed.
* Pane state persists between launches.
* The UI contains no hard-coded Iris personality logic.
* The design can support future personality, voice, history, and tool features without restructuring the entire application.

---

# 22. Example Expected Behavior

User request:

> Find the PDF files related to Phase 4.

Expected Chat response:

> I found three likely matches: a.pdf, b.pdf, and y.pdf. Two are in the Iris project folder, and one is in your general AI documents folder. Would you like me to open one, copy a path, or compare them?

Expected Details content:

```text
Matching files

a.pdf
E:\AI\Iris\Documents
184 KB
Modified July 30, 2026
Matched filename and document content
[Open] [Copy Path] [Open Folder]

b.pdf
E:\AI\Iris\Archive
1.2 MB
Modified July 18, 2026
Matched document content
[Open] [Copy Path] [Open Folder]

y.pdf
E:\AI\Documents
620 KB
Modified June 29, 2026
Matched filename
[Open] [Copy Path] [Open Folder]
```

This example demonstrates the intended division:

* Chat explains the result conversationally.
* Details provides the complete supporting information and controls.

---

# 23. Deliverables

Provide:

1. Updated source code
2. Any new response model files
3. Any new renderer files
4. Updated launch instructions if needed
5. A concise change summary
6. A list of files changed
7. Manual testing steps
8. Any known limitations
9. Confirmation that Phase 1 behavior remains intact
10. Confirmation that the Phase 2 acceptance criteria were tested

Do not begin Phase 3 features as part of this work.
