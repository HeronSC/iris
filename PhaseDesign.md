# Iris – Next Development Phase: Memory, Learning, and State

We are preparing Iris to eventually act as the intelligence/decision layer for the trading bot. Before doing trading-specific integration, Iris needs a general-purpose memory and learning architecture.

The goal is **not to build a trading system yet**.

The goal is to give Iris the infrastructure necessary to observe things over time, remember them, evaluate outcomes, learn from them, and reuse that knowledge later. Trading will be the first major application of this architecture, but the architecture should not be trading-specific.

## Existing Iris

Iris already has:

* Local LLM support through Ollama.
* Document indexing/search.
* Persistent data under `E:\AI\Iris\Data`.
* Current memory/profile information.
* Conversation/topic handling in progress.
* An orchestrator concept intended to decide how requests should be handled.
* Tools/actions that Iris can invoke.

Do not replace working functionality unnecessarily. Extend the existing system.

---

# 1. Build a Real Persistent Memory Layer

Iris needs more than conversation history or a profile JSON file.

Create a persistent memory service that other Iris components can use.

Memory should be capable of representing at least:

### Facts

Things Iris believes to be true.

Example:

```text
The trading scanner normally reduces approximately 13,000 symbols to around 500 candidates.
```

### Observations

Something Iris observed at a particular point in time.

Example:

```text
ABC had RSI 47, volume acceleration 2.1x, price +1.3%, and entered the candidate list at 10:04 AM.
```

### Outcomes

What happened after an observation or action.

Example:

```text
ABC reached +3.4% after being identified.
```

### Decisions

A decision Iris or another system made.

Example:

```text
ABC was rejected because confidence was below the required threshold.
```

### Hypotheses

Something Iris thinks may be true but has not proven.

Example:

```text
Candidates with rapidly increasing relative volume during the first 45 minutes may outperform otherwise similar candidates.
```

### Learned Knowledge

Knowledge that has accumulated enough evidence to become useful in future reasoning.

This should retain information about **why** it was learned and what evidence supports it.

---

# 2. Memory Must Have Provenance

Do not simply save arbitrary LLM statements as facts.

Every memory should, where applicable, contain information such as:

* ID
* memory type
* topic/domain
* timestamp
* source
* content/data
* confidence
* supporting evidence
* related memory IDs
* outcome, if applicable
* status

Useful statuses could include concepts such as:

* observed
* proposed
* testing
* supported
* rejected
* accepted
* superseded

Exact names can change if there is a better design.

The important requirement is that Iris can distinguish:

**"I saw this"**

from

**"I think this"**

from

**"we tested this"**

from

**"we have enough evidence to use this."**

---

# 3. Memory Cannot Just Grow Forever

Build retrieval and relevance into the design.

Iris should be able to ask the memory system questions such as:

```text
What do we know about this topic?
```

```text
What previous observations resemble this one?
```

```text
What hypotheses are currently being tested?
```

```text
What did we learn from similar outcomes?
```

```text
Why do we believe this rule?
```

Memory retrieval should use both structured metadata and semantic relevance where appropriate.

Do not stuff the complete memory database into the LLM prompt.

Retrieve only relevant information.

---

# 4. Add Topic / Working State

Iris needs to understand ongoing work rather than treating every interaction as isolated.

Create a persistent topic/state mechanism.

A topic could be:

```text
Trading Bot
```

with subtopics such as:

```text
Should Buy
Candidate Ranking
Entry Timing
Winner Analysis
```

Or another domain could have completely unrelated topics.

The state system should allow Iris to know:

* what we are currently working on;
* what decisions have already been made;
* unresolved questions;
* active hypotheses;
* next actions;
* relevant memories;
* previous results.

This should survive restarting Iris.

Conversation history alone is not sufficient.

---

# 5. Build the Learning Loop

The core learning process we discussed is:

```text
OBSERVE
   ↓
STORE
   ↓
ANALYZE
   ↓
HYPOTHESIZE
   ↓
TEST
   ↓
MEASURE
   ↓
ADOPT or REJECT
```

Build the architecture around this cycle.

This does NOT mean Iris autonomously modifies its own source code.

Learning means Iris can accumulate evidence and improve its knowledge/model of a problem.

For example:

```text
Observation:
Certain candidate characteristics appear repeatedly among successful trades.

Hypothesis:
Those characteristics may predict better outcomes.

Test:
Evaluate the hypothesis against historical or future candidates.

Measurement:
Compare performance with and without the proposed rule.

Result:
Support, reject, or continue testing the hypothesis.
```

The evidence and test results must remain available so Iris can explain how it reached a conclusion later.

---

# 6. Never Automatically Promote an Idea into Production Behavior

This is important.

Iris can:

* discover patterns;
* propose changes;
* run analysis;
* test hypotheses;
* compare results;
* recommend changes.

Iris should NOT silently change production behavior because it thinks it found something better.

There must be a distinction between:

```text
experimental knowledge
```

and

```text
approved production behavior
```

Eventually we may allow automated promotion under tightly controlled conditions, but do not design around that assumption now.

---

# 7. Generalize the Architecture

Do not create classes such as:

```text
TradingMemory
StockLearningEngine
TradeHypothesis
```

for this foundation unless they are part of a separate trading module.

The central Iris system should deal with concepts such as:

```text
Memory
Observation
Outcome
Hypothesis
Experiment
Evidence
Topic
Decision
Knowledge
```

Then a trading module can use those services.

This is important because Iris should eventually learn about many domains, not only stocks.

---

# 8. Prepare the Orchestrator for This Architecture

The Iris orchestrator eventually needs to determine things such as:

```text
Is the user asking a question?
```

```text
Do I already know something relevant?
```

```text
Should I search documents?
```

```text
Should I retrieve memory?
```

```text
Should I invoke a specialist/tool?
```

```text
Is this new information that should become an observation?
```

```text
Is there an outcome that closes a previous observation or experiment?
```

Do not try to solve every orchestration problem in this task, but make sure the memory/state APIs are designed so the orchestrator can use them cleanly.

---

# 9. Trading Bot Direction

Keep this in mind while designing the system, but **do not build the trading integration yet.**

The existing trading bot remains responsible for the fast operational work:

```text
Market Data
     ↓
Existing Scanner
     ↓
~13,000 symbols
     ↓
~500 candidates
```

Iris will eventually sit above that:

```text
~500 candidates
     ↓
Iris analysis / learning / ranking
     ↓
~50 high-interest candidates
     ↓
Active ranked watch list
     ↓
Trading decision
```

We discussed initially sending something closer to the top **~200 candidates** into Iris while developing the ranking/watch process rather than immediately replacing the current bot.

The existing bot should continue handling things it already does well:

* broker connectivity;
* market feeds;
* order execution;
* fast calculations;
* trade management;
* operational logging.

Iris becomes the **intelligence and decision layer**, not the low-level execution engine.

Eventually the interface should be something conceptually like:

```text
Bot → candidate/market observations → Iris
Iris → ranking / confidence / rationale → Bot
Bot → actual outcome → Iris
Iris → learning system
```

That feedback path is one of the major reasons we need this memory architecture first.

---

# 10. First Implementation Deliverable

For this phase, implement the foundation needed for:

### Persistent structured memory

Create/store/update/retrieve memories.

### Topic state

Persist active topics, decisions, questions, hypotheses, and related information.

### Observation/outcome linkage

An observation can later be associated with its outcome.

### Hypothesis tracking

Create a hypothesis and track evidence for and against it.

### Evidence

Retain the data used to support or reject something.

### Learning status

Allow a hypothesis to progress through testing toward acceptance/rejection without automatically changing production logic.

### Retrieval

Given a topic/current situation, retrieve the memories most relevant to the LLM.

### Auditability

We need to be able to ask:

```text
Why does Iris believe this?
```

and receive an answer traceable back to observations/evidence.

---

# 11. Storage

Prefer a durable structured database rather than trying to put everything into `profile.json`.

Iris already has SQLite infrastructure for its document index, so evaluate whether SQLite is appropriate for this subsystem as well.

It can be a separate database if that produces cleaner separation.

Do not force every piece of information into vector storage. Some information is inherently relational/structured.

Semantic/vector retrieval can complement structured storage.

---

# 12. Architecture Requirement

Separate these concerns cleanly.

Conceptually I would expect something similar to:

```text
Iris
│
├── Orchestrator
│
├── Memory
│   ├── Repository
│   ├── Retrieval
│   └── Relationships
│
├── Topics / State
│
├── Learning
│   ├── Observations
│   ├── Outcomes
│   ├── Hypotheses
│   ├── Evidence
│   └── Experiments
│
├── Tools
│
└── Domain Modules
    └── Trading          <-- later
```

Those exact directories/classes are NOT mandated. Use the current Iris architecture and modify it intelligently rather than forcing this exact tree onto the project.

---

# 13. Before Making Large Changes

First inspect the existing Iris codebase.

Determine:

1. What current memory/state functionality already exists.
2. What can be reused.
3. What needs to be extended.
4. Where the new memory service should live.
5. How the orchestrator should access it.
6. What database/schema changes are required.

Then begin implementing the foundation incrementally.

Do not rewrite Iris from scratch.

The objective of this phase is:

> Give Iris a durable brain that can remember observations, associate them with outcomes, accumulate evidence, test ideas, and retrieve what it has learned.

Once that works, we can attach the trading bot as the first real-world learning domain.
