# Memory & Learning Phase — What Was Built

Companion to `PhaseDesign.md`. That document is the brief; this one records what
exists, what was decided along the way, what is measured, and what is still open.

Delivered in seven commits, `a48b09d` through `2c4a66d`. 359 tests pass.

---

## 1. Against the brief

| Design section | Status | Where |
|---|---|---|
| §1 Persistent memory layer | Done | `core/knowledge/models.py`, `repository.py` |
| §2 Provenance | Done | `MemoryRecord` — source is required, not optional |
| §3 Retrieval, no prompt stuffing | Done | `retrieval.py`, token-budgeted |
| §4 Topic / working state | Pre-existing, reused | `core/conversation/persistent_memory/` |
| §5 Learning loop | Structure done, no trigger | `hypotheses.py` — see Open Questions |
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
core/knowledge/                    1511 lines
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
└── knowledge_commands.py   /knowledge pending | review | why | approve | decline
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
`RELATES_TO` is excluded from that walk, because association is not grounds.

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
| `retrieve()`, 200k records, default limits | — | **4.4 ms** |

The first was an `ORDER BY` the index did not cover, so SQLite sorted every
matching row to return twenty; fixed with an insertion `sequence` column. The
second was a lookup per row; fixed with an anti-join. The 500-row figure is the
one that mattered, because §9 has Iris receiving around 500 candidates.

Query plans are now asserted in tests, not checked once: a reintroduced sort is
invisible until the data is large enough to hurt.

---

## 5. Known limits

**Retrieval selects candidates by recency.** `candidate_limit` bounds what SQL
returns before ranking, so a strong match older than that window is never ranked.
There is a test demonstrating this rather than a note hoping it is remembered.
`candidate_limit_reached` in the diagnostics is the only signal it may have
happened. Fixing it properly needs relevance-aware candidate selection — SQLite
FTS5 or embeddings — and that should follow evidence from real use.

**Ranking is deliberately simple.** Token overlap, a recency half-life, and a
standing prior. The weights are a starting point, exposed as arguments so they
can be moved on evidence. Every score keeps its parts and every retrieval returns
diagnostics, which is the instrumentation that decision will need.

**The store is empty.** Nothing writes observations automatically. Records arrive
only through the API or `/knowledge`.

**None of it has met real data.** Every property is proven against records written
for the tests.

**Orphan on disk:** `Data/Memory/Memory/conversations.db`, 131 KB, dated 3 August,
from an earlier path-joining bug. Nothing reads it.

---

## 6. Open questions

**What should automatically become an observation?** (§8) The store cannot be
useful until something writes to it, and a store that records the wrong things is
worse than an empty one. This is the next real decision.

**What triggers TEST and MEASURE?** (§5) Iris has no scheduler; nothing runs
outside a user turn. The lifecycle was built as a function of evidence plus an
explicit `evaluate()` call precisely so a slash command, a background job, or the
bot's feedback can all drive it without changing that code. The decision is still
open and still cheap.

**Does recall answer real questions well?** The ranker is untuned and the
recency-window limit is real. The diagnostics exist so this can be judged from
use rather than guessed.

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
