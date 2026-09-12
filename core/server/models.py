# File: core/server/models.py

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DecisionIn(BaseModel):
    content: Text
    source: str | None = None
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    data: dict[str, Any] = Field(default_factory=dict)


class ObservationIn(BaseModel):
    content: Text
    topic: str | None = None
    source: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    occurred_at: str | None = None
    source_ref: str | None = None
    decision: DecisionIn | None = None


class ObservationBatch(BaseModel):
    topic: Text
    source: Text
    source_ref: str | None = None
    observations: list[ObservationIn] = Field(min_length=1, max_length=5000)


class RecordOut(BaseModel):
    id: str
    kind: str
    topic: str
    status: str
    content: str
    source: str
    confidence: float | None = None
    occurred_at: str | None = None
    created_at: str


class Written(BaseModel):
    id: str
    decision_id: str | None = None


class BatchAccepted(BaseModel):
    written: int
    topic: str
    ids: list[str]
    records: list[Written]


class OutcomeIn(BaseModel):
    content: Text
    source: Text
    favourable: bool | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class OutcomeItem(BaseModel):
    observation_id: Text
    content: Text
    favourable: bool | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class OutcomeBatch(BaseModel):
    source: Text
    outcomes: list[OutcomeItem] = Field(min_length=1, max_length=5000)


class OutcomesAccepted(BaseModel):
    written: int
    ids: list[str]


class RecallIn(BaseModel):
    text: str = ""
    topic: str | None = None
    topic_prefix: str | None = None
    kinds: list[str] = Field(default_factory=list)
    limit: int = Field(default=10, ge=1, le=200)
    candidate_limit: int = Field(default=400, ge=1, le=5000)
    max_tokens: int | None = None


class ScoredOut(BaseModel):
    record: RecordOut
    score: float
    reasons: dict[str, float] = Field(default_factory=dict)


class RecallOut(BaseModel):
    records: list[ScoredOut]
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class AssessIn(BaseModel):
    ids: list[Text] = Field(min_length=1, max_length=5000)
    record: bool = False


class RuleOut(BaseModel):
    hypothesis_id: str
    content: str
    supporting: int
    contradicting: int
    matched_on: list[str]


class AssessmentOut(BaseModel):
    basis: str
    sample: int
    favourable: int
    unfavourable: int
    score: float | None
    rationale: str
    method: str
    rules: list[RuleOut] = Field(default_factory=list)


class AppraisalOut(BaseModel):
    id: str
    rank: int | None
    assessment: AssessmentOut
    binding: bool
    contract: str


class AssessOut(BaseModel):
    contract: str
    binding: bool
    ranked: int
    recorded: int
    appraisals: list[AppraisalOut]


class CompareIn(BaseModel):
    cohort: Text
    top_n: int = Field(default=50, ge=1, le=5000)


class RankerOut(BaseModel):
    source: str
    scored: int
    top_n: int
    hits: int
    hit_rate: float
    lift: float
    tied_at_the_cut: int
    distinct_scores: int


class CompareOut(BaseModel):
    cohort: str
    size: int
    resolved: int
    base_rate: float | None
    top_n: int
    rankers: list[RankerOut]
    notes: list[str]
    leader: str | None
    summary: str


class Health(BaseModel):
    status: str
    records: int
    assistant: str
    contract: str
    host: str | None = None
    checked_at: str | None = None
    models: dict[str, Any] = Field(default_factory=dict)
    indexed: dict[str, Any] = Field(default_factory=dict)
    databases: dict[str, str] = Field(default_factory=dict)
    watchers: dict[str, Any] = Field(default_factory=dict)
    schedules: dict[str, Any] = Field(default_factory=dict)
    http: dict[str, Any] = Field(default_factory=dict)
    backups: str | None = None
    broken: list[str] = Field(default_factory=list)
