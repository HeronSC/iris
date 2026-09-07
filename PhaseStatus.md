# Memory & Learning Phase — What Was Built

Companion to `PhaseDesign.md`. That document is the brief; this one records what
exists, what was decided along the way, what is measured, and what is still open.

Delivered in ten commits, `a48b09d` through the hypothesis route. 395 tests pass.

---

## 1. Against the brief

| Design section | Status | Where |
|---|---|---|
| §1 Persistent memory layer | Done | `core/knowledge/models.py`, `repository.py` |
| §2 Provenance | Done | `MemoryRecord` — source is required, not optional |
| §3 Retrieval, no prompt stuffing | Done | `retrieval.py`, token-budgeted |
| §4 Topic / working state | Pre-existing, reused | `core/conversation/persistent_memory/` |
| §5 Learning loop | Done, driven by hand | `hypotheses.py`, `/knowledge` — see Open Questions |
| §6 Never auto-promote | Done, enforced in code | `HypothesisTracker.promote` |
| §7 Generalised, not trading-specific | Done | nothing in `core/knowledge/` mentions trading |
| §8 Orchestrator can reach it | Done | `core/assistant/knowledge_provider.py` |
| §10 Deliverables | Done | all seven items |
| §11 SQLite, separate database | Done | `Data/Memory/knowledge.db` |
| §12 Separation of concerns | Done | module map below |

What §10 asked for, and where it lives:

- **Create/store/update/retrieve** — `KnowledgeRepository`
- **Topic state** — the existing `TopicMemoryService`, left in place
- **Observation → outcome linkage** — `KnowledgeGraph.record_outcome` / `outcome_for`
- **Hypothesis tracking** — `HypothesisTracker`
- **Evidence** — `KnowledgeGraph.add_evidence` / `evidence_for`
- **Learning status without changing behaviour** — statuses, with `promote()` as the only door to `accepted`
- **Retrieval** — `KnowledgeRetriever`
- **Auditability** — `KnowledgeGraph.explain` / `render_explanation`

---

## 2. The shape of it

```
core/knowledge/                    1774 lines
├── models.py         MemoryRecord, MemoryKind, MemoryStatus
├── schema.py         all DDL in one place, idempotent
├── repository.py     append-only record store
├── links.py          typed edges; evidence is a relation, not a table
├── graph.py          records + edges: outcomes, evidence, explain()
├── ranking.py        tokeniser and scorer for this content
├── retrieval.py      structured query, bounded and budgeted
├── hypotheses.py     the lifecycle and the promotion gate
└── review.py         the approval queue

core/assistant/
├── knowledge_provider.py   recall, as a capability the planner may choose
└── knowledge_commands.py   the whole loop, as slash commands
```

The loop, end to end, without opening Python:

```
/knowledge observe <topic> <what you saw>        OBSERVE
/knowledge outcome <id> <what happened>          MEASURE
/knowledge hypothesize <topic> <claim>           HYPOTHESIZE
/knowledge evidence <id> for|against <id>        TEST
/knowledge testing | pending | review            what is where
/knowledge approve <id> | decline <id> <reason>  ADOPT or REJECT
/knowledge why <id>                              the audit trail
```

Three namespaces now mean three different things, which is why `core.memory` was
renamed during this phase:

- `core.profile` — the curated profile, preferences, projects. Approval-gated.
- `core.conversation.persistent_memory` — conversation topics and working state.
- `core.knowledge` — observations, outcomes, hypotheses, evidence. This phase.

---

## 3. Decisions worth remembering

**Records are append-only.** There is no method that edits content. A revision is
a new record superseding the old one, and only `status` and `superseded_by` are
ever updated in place. "Why does Iris believe this" is answerable only if what it
believed earlier is still on disk, so this is enforced by the absence of an
update path rather than by convention.

**Evidence is a relation, not a table.** The plan called for a separate `evidence`
table. Building it showed that table would duplicate the link columns to answer
questions a relation already answers, so `SUPPORTED_BY` and `CONTRADICTED_BY` are
relations and `evidence_for` is a query.

**Every edge points from a claim to what it rests on.** The evidence relations are
named from the hypothesis's side deliberately: it makes provenance one uniform
walk along outgoing edges instead of a different direction per relation.
`RELATES_TO` is the only relation excluded, because association is not grounds.
`OUTCOME_OF` was excluded too until the loop was driven end to end and `why`
was seen stopping at the measurement, one hop short of what was actually
observed — which is the hop §10 asks for.

**Evidence is an existing record, never text typed at the moment of linking.**
What argues for an idea has to have been recorded in its own right, with its own
source and timestamp; a sentence typed into the evidence command would let a
claim be justified by a restatement of itself. Another hypothesis is refused for
the same reason: verdicts count edges, so a belief grounded in a belief would
let two unproven ideas support each other into being supported.

**Evidence may demote, never reopen.** Filing evidence re-reads the verdict only
while a hypothesis is still proposed, testing or supported — the statuses
evidence reached on its own. Accepted and rejected are where a person put it, and
walking those back would undo a decision nobody was asked about; the link is
still written and the reply says the verdict was not applied. Pulling something
back out of the approval queue is the safe direction, so `refresh()` covers
supported as well.

**Verdicts run on counts, never on confidence.** `MemoryRecord` has a `confidence`
field and an LLM will fill it with 1.0. Nothing in the lifecycle reads it. A test
asserts that a maximally confident claim with one supporting outcome still sits
in testing.

**Promotion needs a name.** `promote(approved_by=...)` is keyword-only with no
default, so it cannot be called without someone standing behind it. `evaluate()`
can never reach `accepted` however one-sided the evidence gets — a test drives 50
supporting outcomes against zero and the verdict stays `supported`.

**The approval machinery was not generalised.** The plan said to extend
`MemoryProposalStore` / `MemoryUpdateService` to carry promotions. Reading them
showed how profile-shaped they are: a JSON patch with `memory_area`, `operation`,
`target_id`, over four hardcoded files. Generalising would have made
`MemoryProposal` a union type serving two unrelated operations. The two decisions
also differ in kind — "remember that you prefer X" is small and frequent,
"start ranking candidates by relative volume" is rare and consequential. The
shared piece is the audit log: both paths write to the same `AuditLogger`.

**Recall has no keyword route.** `can_handle` returns `False`. The planner chooses
it or it is not reached, which is the direction set when the weather
special-casing was removed.

---

## 4. Measured, not assumed

Three scaling faults were found by measuring rather than by review. All were
invisible at test-data size.

| What | Before | After |
|---|---|---|
| `list_by_topic`, 100k records | 25.2 ms | **0.83 ms** |
| `open_observations(limit=500)`, 20k records | 163.4 ms | **2.9 ms** |
| `retrieve()`, 200k records, distinguishing term | — | **~4 ms** |
| `retrieve()`, 200k records, every term in every record | — | **~140 ms** |

The first was an `ORDER BY` the index did not cover, so SQLite sorted every
matching row to return twenty; fixed with an insertion `sequence` column. The
second was a lookup per row; fixed with an anti-join. The 500-row figure is the
one that mattered, because §9 has Iris receiving around 500 candidates.

Query plans are now asserted in tests, not checked once: a reintroduced sort is
invisible until the data is large enough to hurt.

A fourth fault was a hand-rolled one. Candidate selection ordered by recency, so
a strong older match was unreachable however well it matched; this was written up
as a limitation needing "a text index" and a future decision about embeddings.
SQLite ships FTS5 with BM25 in the standard library, and it was there the whole
time. Candidate selection now uses it, the scorer still re-ranks on recency and
standing, and the limitation is gone. The cost moved rather than vanishing: it
now tracks how many records match the query instead of being flat, which is the
right shape but slower for a query that matches everything.

---

## 5. Known limits

**Ranking is deliberately simple.** Token overlap, a recency half-life, and a
standing prior. The weights are a starting point, exposed as arguments so they
can be moved on evidence. Every score keeps its parts and every retrieval returns
diagnostics, which is the instrumentation that decision will need.

**Nothing writes to the store on its own.** Every record arrives because a
person typed it or called the API. The loop runs, but a hand turns it.

**None of it has met real data.** Every property is proven against records written
for the tests.

**Orphan on disk:** `Data/Memory/Memory/conversations.db`, 131 KB, dated 3 August,
from an earlier path-joining bug. Nothing reads it.

---

## 6. Open questions

**What should automatically become an observation?** (§8) Stage one was explicit
typing, and it is what the loop runs on today. Stage two is LLM-proposed
observations, which have an obvious home: the same review gate promotions go
through. Stage three is the bot feed. A store that records the wrong things is
worse than an empty one, which is why using the typed path first is the point
rather than a delay.

**What triggers TEST and MEASURE without a person?** (§5) A person now triggers
them: filing evidence re-reads the verdict, and `/knowledge review` re-reads every
open one. Iris still has no scheduler, so nothing re-assesses between turns. The
lifecycle is a function of evidence plus an `evaluate()` call, so a background job
or the bot's feedback can drive the same code when there is one.

**Does recall answer real questions well?** Candidates now come from SQLite's
search index, but the re-ranking weights and the relevance floor are still
untuned guesses. The diagnostics exist so this can be judged from use rather
than argued about.

---

## 7. What §9 will still need

The trading interface is deliberately not built. When it is, the missing pieces
are:

1. **An inbound path.** Iris has an interactive loop and no API. The bot cannot
   hand it candidates today.
2. **A batch write.** Records go in one at a time; 500 candidates per morning
   wants a transaction, not 500.
3. **A ranking output contract.** §9 wants ranking, confidence and rationale back.
   `explain()` produces the rationale; the ranking and confidence shapes are
   undecided.
4. **Outcome reconciliation.** `record_outcome` links one outcome to one
   observation by id. The bot will report outcomes by symbol and time, so
   something must match them up.

The domain module belongs outside `core/knowledge/`, consuming these services.
Nothing trading-specific has entered core, which was §7's requirement.
