Pause feature work: build the AI orchestrator first

Do not continue improving weather, forecasting, file search, or any other capability by adding more keyword lists, phrase checks, or capability-specific conversation parsing.

The current direction is wrong for Iris.

Iris is intended to be an AI assistant where the LLM understands the conversation and decides what capability is needed. The Python code should provide capabilities and execute them. It should not attempt to understand normal English through growing if "<phrase>" in text logic.

Immediate instruction

Stop adding branches to methods such as:

_parse_weather_request(text)
can_handle(text)

Do not extend those methods with more phrases.

Do not treat the existing parser as something to improve.

Treat it as temporary legacy code that will be replaced by a central orchestrator.

New phase goal

Build a reusable AI orchestration layer that works for every capability.

The orchestrator must:

Receive the user message and relevant conversation context.
Receive the list of available capabilities and their schemas.
Ask the LLM to determine one of three outcomes:
respond conversationally without a tool;
call a capability with structured arguments;
ask a necessary clarification.
Validate the structured capability request.
Execute the selected provider.
return structured facts to the LLM.
Generate a complete chat response.
Render optional structured details separately.
Preserve active topic and capability state for follow-ups.
Required capability contract

Every capability should be registered using a common definition similar to:

class CapabilityDefinition:
    name: str
    description: str
    request_schema: type
    result_schema: type
    executor: CapabilityExecutor
    confirmation_policy: ConfirmationPolicy

Example:

WeatherCapability(
    name="weather",
    description=(
        "Retrieve current weather and forecasts for a location, "
        "including hourly, daily, weekend, and multi-day outlooks."
    ),
    request_schema=WeatherRequest,
    result_schema=WeatherResult,
)

The LLM sees the description and schema. It decides that:

“What is it like outside?”

means something like:

{
  "capability": "weather",
  "arguments": {
    "location": "default",
    "range": "current",
    "granularity": "current"
  }
}

And that:

“What about tomorrow?”

means:

{
  "capability": "weather",
  "arguments": {
    "location": "same_as_active_topic",
    "range": "tomorrow",
    "granularity": "daily"
  }
}

No new Python phrase is required.

Strict provider boundary

Providers must not receive raw conversational text.

Wrong:

weather.execute(user_text)

Correct:

weather.execute(weather_request)

The provider may validate, normalize, fetch, and return data. It may not interpret the user’s wording.

Apply this same rule to all future providers:

calendar.execute(CalendarRequest(...))
email.execute(EmailRequest(...))
files.execute(FileSearchRequest(...))
stocks.execute(StockRequest(...))
smart_home.execute(DeviceActionRequest(...))
Central tool-selection prompt

Create one central LLM orchestration prompt. Do not create a separate phrase parser for each capability.

The prompt should include:

current user message;
relevant recent conversation;
active topic;
active capability;
known entities and defaults;
pending confirmation state;
available capabilities;
each capability’s structured schema.

The model must return constrained structured output, not free-form guesses.

For example:

{
  "decision": "tool",
  "capability": "weather",
  "arguments": {
    "location": "Anderson, South Carolina",
    "range": "week",
    "granularity": "daily"
  },
  "confidence": 0.96
}

Or:

{
  "decision": "respond"
}

Or:

{
  "decision": "clarify",
  "question": "Which account do you mean?"
}
Topic continuity

Follow-ups must use semantic state.

Store at least:

class TopicState:
    topic_id: str
    capability: str | None
    entities: dict
    filters: dict
    selections: list
    exclusions: list
    last_request: object | None
    last_result: object | None
    retrieved_at: datetime | None

The phrase:

“What about tomorrow?”

should be interpreted using the active topic.

It must not be routed based solely on the word tomorrow.

Likewise:

“Remove the second one.”

should update the current topic state and preserve that exclusion in future requests.

Chat and Details

The orchestrator must produce two independent outputs from the same structured result:

Structured Result
    ├── Chat Response
    └── Details View

The chat response must fully answer the user’s question even when Details is hidden.

Details may add:

complete records;
exact values;
source information;
timestamps;
metadata;
actions;
deeper explanation.

Chat must never merely announce that details exist.

Temporary compatibility code

Existing phrase-based routing may remain only as an isolated fallback while the orchestrator is being completed.

Requirements for that fallback:

do not add new phrases;
mark it deprecated;
keep it outside providers;
log whenever it is used;
remove it after orchestrator acceptance tests pass.
Acceptance tests

Do not consider this phase complete until these work without adding new phrase rules:

Weather
“What is it like outside?”
“What about tomorrow?”
“How does the weekend look?”
“Would Saturday be a good day to mow?”
Files
“Find the spreadsheet I made about E*TRADE last month.”
“Open the second one.”
“No, remove that one from the results.”
“What was the other Excel file?”
Calendar
“What do I have tomorrow?”
“What about the afternoon?”
“Move the later one to Friday.”
Cross-topic switching
“What is tomorrow’s forecast?”
“What meetings do I have tomorrow?”
“What about the afternoon?”

The final question must remain a calendar follow-up, not be routed to weather.

No code changes for phrasing variations

These should map to the same capability without modifying Python:

“Will it rain?”
“Do I need an umbrella?”
“Is mowing tomorrow a bad idea?”
“How ugly is the weather going to be?”
Definition of failure

The architecture is still wrong if:

providers receive raw user sentences;
adding a new phrasing requires editing a Python list;
each capability has its own natural-language parser;
active context is inferred mainly from keywords;
broad terms such as today, tomorrow, outside, or what about choose a provider;
the LLM is used only to reword results after Python has already decided everything.

Before writing code, provide:

The proposed orchestrator design.
The capability interface.
The structured LLM decision schema.
The topic-state model.
The migration plan away from current phrase parsing.
The acceptance-test plan.

Do not implement another weather-specific patch before this design is reviewed.

That last line matters. Ask him for the architecture first, not code first.

Right now Cody is likely behaving rationally: he sees a failing feature and repairs the nearest code path. You need to explicitly change the assignment from:

Fix the current behavior.

to:

Stop patching the current behavior. Design the reusable decision layer that all behavior will pass through.

I would make this a distinct phase and reject any pull request that adds more natural-language phrases to a provider.