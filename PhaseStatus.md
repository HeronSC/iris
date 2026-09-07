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

core/knowledge/
├── appraisal.py      the shadow-1 assessment: counts, never a claimed confidence
└── comparison.py     hit rate at top N, between whoever scored the same cohort

core/server/
├── models.py         the wire shapes, validated at the edge
└── app.py            the endpoints, over the knowledge services
serve.py              the headless front-end
```

Three front-ends now share one `IrisApplication`: `core/main.py` reads stdin,
`ui/main.py` runs a Qt window, `serve.py` serves HTTP. None of them hold
business logic.

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
| 500 records written, empty store | 2913 ms | **20 ms** |
| 500 records written, 20k already stored | — | **43 ms** |
| 500 candidates POSTed over HTTP, end to end | — | **343 ms** |

The first was an `ORDER BY` the index did not cover, so SQLite sorted every
matching row to return twenty; fixed with an insertion `sequence` column. The
second was a lookup per row; fixed with an anti-join. The 500-row figure is the
one that mattered, because §9 has Iris receiving around 500 candidates.

The last row is `add_many`, the batch write §9 needs. `add` opened a connection,
set two pragmas, checked for a clashing id, took a MAX over the table and
committed — per record, and with `journal_mode=DELETE` each commit is a journal
file created and unlinked. A morning of 500 candidates cost 2.9 seconds of
almost entirely fixed overhead. One connection, one sequence lookup, one
`executemany` and one commit make it 20 ms. `add` is now the one-record case of
`add_many`, so there is a single write path rather than two that can drift.

The search index needed no work: it hangs off an `AFTER INSERT` trigger, which
fires per row inside the batch like any other insert. A test asserts a
batch-written record is findable, because that is the kind of thing a later
change to the write path would break silently.

**WAL was tried and rejected on measurement.** Two Iris processes sharing one
database looked like a reason to move off `journal_mode=DELETE`. Measured, WAL
was worse: 1843 ms against 546 ms for two thousand connect-query-close cycles.
`SQLiteDatabase.connect()` opens a fresh connection per operation, and WAL pays
a shared-memory setup cost per connection that DELETE does not. The same test
with one reused connection inverts it completely -- 7 ms against 117 ms, WAL
sixteen times faster. So the journal mode was never the lever; connection
lifetime is. DELETE stays until connections are reused, and `busy_timeout` is
now set explicitly rather than inherited from the driver default, because the
value matters once two processes write.

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

**Similarity is the weak link, and it is measured.** `Appraiser` finds
comparable observations through the same text index retrieval uses. On seeded
data where the favourable rate genuinely ran from 0.416 to 0.871 by one
structured field, the appraisals came back spanning 0.489 to 0.612 -- about a
quarter of the available signal. The cause is not subtle: every candidate's
content is close to the same sentence, and what actually separates them lives in
`data`, which BM25 barely sees. Shipping it anyway is the point of shadow mode,
since "what counts as comparable" is the question a season of real data answers
and argument does not. It is also why nothing may act on the number yet.

**Iris's scores are too coarse to order a morning.** The comparison reports how
many candidates sit tied at the cut, and for Iris that ran from 18 to 77 out of
150. Its score is a favourable rate over a neighbour set, so many candidates
land on identical values and which of them makes the top N is close to
arbitrary. Granularity, not just accuracy, is what the ranking lacks.

**A comparison is only honest if the assessment was recorded when it was made.**
Re-appraising a cohort after its outcomes are in would let Iris grade itself with
the answers, and would flatter it badly. So `POST /assess` takes `record: true`
and writes each appraisal as a decision record beside the bot's, both hanging off
the same observation by `DECIDED_FROM`, distinguished by source. The comparison
reads only what was written at the time. An unrecorded assessment simply does not
appear, and the summary says so rather than quietly leaving a ranker out.

**`POST /assess` costs one retrieval per record.** 200 records in 1.3 seconds,
so a 500-candidate morning is a few seconds. Fine for a batch that runs once,
wrong for anything interactive, and the first thing to fix if assessment moves
into the trading loop.

**Ranking is deliberately simple.** Token overlap, a recency half-life, and a
standing prior. The weights are a starting point, exposed as arguments so they
can be moved on evidence. Every score keeps its parts and every retrieval returns
diagnostics, which is the instrumentation that decision will need.

**Nothing writes to the store on its own.** Every record arrives because a
person typed it or called the API. The loop runs, but a hand turns it.

**None of it has met real data.** Every property is proven against records written
for the tests.

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

**Is Iris's ranking worth trusting?** Now a measurable question rather than an
argument. `POST /compare` gathers a cohort by `source_ref`, takes each ranker's
top N, and reports the hit rate against the base rate. Lift is the number that
matters: beating a 100% base rate is not skill. On seven simulated mornings the
bot led every one at +23.0% mean lift against Iris's +2.6%, which is the answer
the metric exists to give and the reason nothing acts on Iris yet.

**Does recall answer real questions well?** Candidates now come from SQLite's
search index, but the re-ranking weights and the relevance floor are still
untuned guesses. The diagnostics exist so this can be judged from use rather
than argued about.

---

## 7. What §9 will still need

The trading interface is deliberately not built. When it is, the missing pieces
are:

1. ~~**An inbound path.**~~ Done. `serve.py` is a third front-end beside the
   console loop and the Qt window, serving the same `IrisApplication` over HTTP
   on `127.0.0.1:8765`. `POST /observations` takes a batch, `GET
   /observations/open` lists what is unclosed, `POST
   /observations/{id}/outcome` closes one, `POST /recall` retrieves. 500
   candidates in, over the wire, in 343 ms.
2. ~~**A batch write.**~~ Done. `KnowledgeRepository.add_many` writes a batch in
   one transaction, all or nothing: a half-written morning is worse than a
   failed one that can be retried. Ids that clash, inside the batch or against
   what is stored, are refused before anything is written.
3. ~~**A ranking output contract.**~~ Shipped as `shadow-1`, live and inert.
   `POST /assess` returns, per record, a basis, the counts behind it, an
   observed rate, a rationale, and `binding: false`. The bot logs it beside its
   own number and acts on neither. Trusting it later is itself a hypothesis,
   and goes through the promotion gate like anything else.
4. ~~**Outcome reconciliation.**~~ Mostly dissolved. The bot POSTs candidates
   and gets their ids back, so it closes by id from its own map and no fuzzy
   matching on symbol and time is needed. `POST /outcomes` closes a whole
   morning in one transaction, which matters because the candidates nobody
   traded are the counterfactuals worth learning from. The matching problem
   only returns if the bot cannot hold that map across a restart.

The domain module belongs outside `core/knowledge/`, consuming these services.
Nothing trading-specific has entered core, which was §7's requirement.
