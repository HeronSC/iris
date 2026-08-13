Iris Persistent Conversation and Topic Memory
Objective

Add persistent conversation history and automatic topic-based recall to Iris.

Closing and reopening Iris must not erase previous discussions. When the user asks a question related to an earlier topic, Iris should retrieve information from all sufficiently relevant prior topics and include that information in the model context.

The user should not normally need to reopen a specific conversation manually or repeat earlier questions.

Core behavior

Iris must:

Save every user and assistant message.
Group messages into logical topics.
Preserve topics between application sessions.
Maintain a concise summary for each topic.
Search previous topics before every normal model request.
Retrieve several relevant topics, not only the highest-scoring topic.
Keep retrieved topics clearly separated and labeled.
Inject the retrieved context into the Ollama request.
Continue the current topic when appropriate.
Start a new topic when the current message is not sufficiently related to existing topics.
Example

Earlier session:

User:
I need a touchscreen laptop with a flip screen, 32 GB RAM, and a 1 TB SSD.

Iris:
Suggested HP Spectre x360, Lenovo ThinkPad X1 Yoga, and Dell XPS 2-in-1.

The user closes Iris.

Later session:

User:
How does the HP OmniBook 7 Flip compare?

Iris should retrieve the earlier laptop-shopping topic and understand that the comparison is against the previously suggested laptops.

The user should not need to repeat the original requirements.

Storage model

Use SQLite unless the current Iris architecture already has a suitable persistent database abstraction.

Recommended tables are below.

conversations

Represents an application conversation or UI thread.

id
title
created_at
updated_at
is_archived
topics

Represents a persistent subject that may span multiple conversations and application sessions.

id
name
summary
created_at
updated_at
last_active_at
embedding
status

Suggested status values:

active
inactive
archived

The embedding may be stored directly or in a companion vector-storage implementation.

messages
id
conversation_id
topic_id
role
content
created_at
sequence_number

Suggested roles:

user
assistant
system
tool
topic_links

Optional, but recommended for future use.

id
source_topic_id
target_topic_id
relationship
created_at

Examples of relationships:

related
depends_on
part_of
contradicts

Do not merge topics merely because both were retrieved for one request.

Active conversation behavior

The UI should create or reopen a conversation record when Iris starts.

A new application launch may create a new conversation, but previous topics remain available to memory retrieval.

The distinction is:

Conversation:
A UI or application session/thread.

Topic:
A persistent subject that may continue across many conversations.

Example:

Conversation 14
    Laptop Shopping topic

Conversation 19
    Laptop Shopping topic
    Iris Server Hardware topic

A topic is not deleted when a conversation ends.

Topic assignment

Before sending a normal user message to Ollama, Iris should determine whether the message:

Continues the current active topic.
Matches one or more prior topics.
Starts a new topic.

Use a combination of:

current conversation context;
recent messages;
topic summaries;
embeddings or semantic similarity;
explicit wording and entity overlap;
recency as a secondary factor.

Do not rely entirely on keyword matching.

Initial topic assignment strategy

For the first implementation, use:

Current topic similarity
    +
Semantic search across topic summaries
    +
Recent topic activity

A proposed scoring model:

final_score =
    semantic_similarity * 0.75
    + current_topic_bonus
    + recency_bonus

The exact weights should be configurable rather than hard-coded throughout the code.

Multiple-topic retrieval

Iris must retrieve information from several relevant topics when several topics match.

Do not use only the single highest-scoring result.

Recommended defaults:

Maximum retrieved topics: 5
Minimum relevance score: configurable
Maximum total memory tokens: configurable
Always include current topic when one exists

A reasonable initial configuration might be:

{
  "memory": {
    "enabled": true,
    "max_retrieved_topics": 5,
    "minimum_topic_score": 0.68,
    "maximum_context_tokens": 2500,
    "include_current_topic": true
  }
}

These are initial values, not mandatory final numbers.

Important rule

Retrieved topics must remain clearly separated.

Bad:

The laptop needs 32 GB, the server needs 128 GB, and the room node needs 16 GB.

Better:

Laptop Shopping:
- Laptop requirement is 32 GB RAM.

Iris Server Hardware:
- Dedicated server design discussed 128 GB RAM.

Room Node Hardware:
- Room-node hardware discussed 8–16 GB RAM.

This lets the model distinguish similar facts that belong to different subjects.

Prompt construction

Build a structured context block before the current user message.

Example:

PERSISTENT MEMORY CONTEXT

CURRENT TOPIC
Topic: Laptop Shopping
Last active: 2026-08-03

Summary:
- User wants a touchscreen convertible laptop.
- Required memory: 32 GB RAM.
- Required storage: 1 TB SSD.
- Previously suggested models:
  - HP Spectre x360
  - Lenovo ThinkPad X1 Yoga
  - Dell XPS 2-in-1

RELATED TOPICS

Topic: HP Laptop Research
Relevance: 0.84
Last active: 2026-08-01

Summary:
- User was evaluating HP convertible models.
- HP OmniBook 7 Flip was mentioned as another option.

Topic: Iris Server Hardware
Relevance: 0.70
Last active: 2026-07-30

Summary:
- Dedicated Iris server may use 128 GB RAM.
- This is separate from the laptop purchase.

END PERSISTENT MEMORY CONTEXT

CURRENT USER MESSAGE
How does the HP OmniBook 7 Flip compare?

Do not expose similarity scores to the user unless diagnostics are enabled.

Topic summaries

Do not send complete historical conversations with every request.

Each topic should have a bounded summary containing:

the subject;
user goals;
important requirements;
decisions;
rejected options;
unresolved questions;
relevant entities;
important dates when needed.

Example:

Topic: Laptop Shopping

Goal:
Purchase a convertible Windows laptop.

Requirements:
- Touchscreen
- 360-degree flip screen
- 32 GB RAM
- 1 TB SSD

Previously discussed:
- HP Spectre x360
- Lenovo ThinkPad X1 Yoga
- Dell XPS 2-in-1
- HP OmniBook 7 Flip

Open questions:
- Current pricing
- Best display
- Whether RAM is soldered or upgradeable
Summary update behavior

Update the topic summary:

after a configurable number of new messages;
when the topic changes materially;
when a decision is made;
before the topic is unloaded from active memory;
when the application closes cleanly.

Avoid regenerating the summary after every message unless performance proves acceptable.

Suggested default:

Update after 4–8 new messages or when a significant fact changes.
Supporting message retrieval

Topic summaries may omit details.

When the user asks a precise follow-up, Iris should optionally retrieve supporting message excerpts from the strongest topics.

Example:

User:
Why did we reject the Lenovo?

The topic summary may only say:

Lenovo was not preferred.

Iris should search messages within that topic for statements about:

Lenovo
rejected
display
battery
price
RAM

Then include a small number of relevant excerpts.

Recommended limits:

Maximum supporting excerpts per topic: 3
Maximum excerpt length: configurable
Maximum total evidence tokens: configurable

Do not load the entire topic history unless explicitly requested.

Recent conversational context

Persistent topic recall does not replace ordinary short-term conversation history.

Each request should contain:

System instructions.
Recent messages from the current conversation.
Current topic summary.
Related topic summaries.
Supporting excerpts when needed.
Current user message.

Recent messages should remain in chronological order.

Suggested initial recent-message window:

Last 8–16 messages, bounded by tokens.
Topic switching

Iris should detect obvious topic changes.

Example:

User:
How does the OmniBook compare?

User:
Now help me fix my Azure Function deployment.

The second message should start or resume an Azure-related topic rather than contaminating the laptop topic.

A topic switch should not erase the prior active topic.

The UI conversation can contain several topics, but messages must retain their assigned topic_id.

Ambiguity behavior

When multiple topics are relevant, Iris should use all relevant context when practical.

Example:

User:
How much RAM did we decide on?

Retrieved topics:

Laptop Shopping:
32 GB

Iris Server:
128 GB

Room Node:
8–16 GB

Expected response:

For the laptop, the requirement was 32 GB. We discussed 128 GB for the dedicated Iris server and 8–16 GB for the room nodes.

If the current message does not provide enough information to identify the intended topic, Iris should explain the alternatives rather than silently choosing one.

User-visible conversation restoration

Automatic topic recall is the primary behavior, but the user should also be able to view prior conversations.

Add a basic conversation-history interface or command.

Possible commands:

/conversations
/topics
/topic <name or id>
/resume <conversation id>
/new

The exact interface can follow the current Iris command architecture.

Minimum functionality:

list recent conversations;
list recent topics;
open or resume a conversation;
start a new conversation;
view a topic summary;
archive a conversation or topic later.

This should not be required for normal recall.

New conversation behavior

Starting a new conversation should:

clear the visible current-thread message history;
create a new conversation record;
not erase topics;
not disable memory retrieval;
allow related prior topics to be recalled automatically.

A new conversation means:

Start a clean visible thread.

It does not mean:

Forget everything previously discussed.
Failure behavior

Memory retrieval must not prevent Iris from answering.

If topic search, embedding generation, or database access fails:

Log the error.
Continue with recent in-memory conversation context.
Return the model response normally when possible.
Do not display raw internal errors unless diagnostics are enabled.

A memory failure should degrade gracefully rather than cause an Ollama request failure.

Logging and diagnostics

Add structured diagnostic logging for:

conversation_id
current_topic_id
candidate_topics
similarity scores
selected topics
discarded topics
token budget
supporting excerpts
summary updates
topic switches
database errors
embedding errors

Add a diagnostic mode that can show something like:

Memory retrieval:
- Laptop Shopping: 0.91, included
- HP Laptop Research: 0.82, included
- Iris Server Hardware: 0.63, excluded below threshold

This should be disabled in the standard user interface.

Configuration

Add a dedicated memory configuration section.

Example:

{
  "memory": {
    "enabled": true,
    "database_path": "E:\\AI\\Iris\\Data\\Memory\\conversations.db",
    "max_recent_messages": 12,
    "max_retrieved_topics": 5,
    "minimum_topic_score": 0.68,
    "maximum_memory_tokens": 2500,
    "maximum_supporting_excerpts": 6,
    "summary_update_message_count": 6,
    "include_current_topic": true,
    "diagnostics": false
  }
}

Use Iris's existing external data-path conventions. Do not place persistent data inside source-code or deployment folders.

Suggested service boundaries

Keep this functionality outside the UI layer.

Recommended components:

ConversationRepository
TopicRepository
MessageRepository
ConversationService
TopicMemoryService
TopicClassifier
TopicRetriever
TopicSummaryService
MemoryContextBuilder
TokenBudgetManager
EmbeddingService

Responsibilities:

ConversationService
Starts conversations.
Resumes conversations.
Stores messages.
Tracks current conversation.
TopicMemoryService
Coordinates topic assignment and retrieval.
Tracks the active topic.
Requests summary updates.
TopicClassifier
Decides whether a message continues, resumes, or starts a topic.
TopicRetriever
Searches all stored topic summaries.
Returns multiple ranked matches.
TopicSummaryService
Creates and updates bounded topic summaries.
MemoryContextBuilder
Builds the structured persistent-memory prompt section.
TokenBudgetManager
Prevents retrieved memory from overflowing the model context.
EmbeddingService
Generates embeddings using the selected local embedding model or current supported provider.

Do not place database access, retrieval logic, or summarization logic directly in the desktop UI.

Processing flow

For each normal user message:

1. Receive user message.
2. Persist the user message provisionally.
3. Load recent current-conversation messages.
4. Determine current-topic relationship.
5. Search all topic summaries.
6. Retrieve current topic plus several relevant topics.
7. Optionally retrieve supporting message excerpts.
8. Apply token limits.
9. Build the model prompt.
10. Call Ollama.
11. Persist the assistant response.
12. Assign both messages to the correct topic.
13. Update topic activity.
14. Update the topic summary when required.
15. Return the response to the UI.

Topic assignment may need to occur provisionally before the model call and be corrected afterward if the response reveals a clearer classification.

Important implementation constraints
Preserve the existing Iris application boundary.
Do not make the UI responsible for memory retrieval.
Do not load every old message into Ollama.
Do not merge several topics into one summary.
Do not retrieve only the top topic.
Do not let weak matches consume the entire context.
Do not erase persistent memory when starting a new conversation.
Do not depend solely on exact keywords.
Do not let memory-system failures block ordinary chat.
Keep all limits configurable.
Preserve the original messages as the source of truth.
Summaries are derived data and may be regenerated.
Acceptance tests
Test 1: Restart continuity
Ask Iris for laptop recommendations with detailed requirements.
Close Iris.
Reopen Iris.
Ask, “How does the HP OmniBook 7 Flip compare?”

Expected:

Iris uses the earlier laptop requirements and recommendations without requiring them to be repeated.

Test 2: Multiple matching topics

Create topics containing:

Laptop: 32 GB RAM
Server: 128 GB RAM
Room node: 8–16 GB RAM

Ask:

How much RAM did we decide on?

Expected:

Iris explains all relevant values and identifies which project each belongs to.

Test 3: Clear topic switch

Discuss laptops, then ask about an Azure Function deployment.

Expected:

Iris creates or resumes an Azure topic and does not add the Azure discussion to the laptop topic.

Test 4: Related-topic retrieval

Discuss the HP OmniBook in one conversation and general laptop requirements in another.

Expected:

A later OmniBook question retrieves both topics.

Test 5: Weak match exclusion

Create several unrelated topics that contain the word “memory.”

Expected:

A laptop RAM question should not automatically load software-memory or personal-memory topics unless their semantic score meets the threshold.

Test 6: Application restart

Restart both the UI and Python process.

Expected:

Conversations, topics, messages, summaries, and topic links remain available.

Test 7: Memory subsystem failure

Temporarily make the memory database unavailable.

Expected:

Iris logs the failure and still attempts a normal Ollama response using current in-memory context.

Test 8: Token limit

Create many relevant topics.

Expected:

Iris includes the most useful topics within the configured token budget without exceeding the model context.

Recommended delivery sequence
Phase A: Persistence
Store conversations.
Store messages.
Restore conversation history.
Add /conversations, /resume, and /new.
Phase B: Topics
Add topic records.
Assign messages to topics.
Maintain current topic.
Generate topic summaries.
Phase C: Retrieval
Create topic embeddings.
Search and rank topics.
Retrieve several matches.
Build labeled memory context.
Phase D: Evidence and refinement
Retrieve supporting message excerpts.
Add token budgeting.
Improve topic switching.
Add diagnostics and acceptance tests.

Do not try to solve topic persistence, semantic retrieval, summarization, evidence selection, and UI history in one large code change. Delivering them as isolated stages will make failures much easier to diagnose.