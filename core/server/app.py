# File: core/server/app.py

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from core.knowledge import KnowledgeError, KnowledgeQuery, MemoryKind, MemoryRecord
from core.knowledge.appraisal import CONTRACT, FAVOURABLE_KEY, Appraisal, Appraiser
from core.knowledge.comparison import compare_rankers, render_comparison
from core.knowledge.links import MemoryLink, MemoryRelation
from core.server.auth import ApiAuthenticator, AuthSettings, token_from_headers
from core.server.models import (
    AppraisalOut,
    AssessIn,
    AssessmentOut,
    AssessOut,
    BatchAccepted,
    CompareIn,
    CompareOut,
    Health,
    ObservationBatch,
    OutcomeBatch,
    OutcomeIn,
    RecallIn,
    RecallOut,
    RankerOut,
    RecordOut,
    RuleOut,
    ScoredOut,
    Written,
)

logger = structlog.get_logger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

HEALTH_DETAIL_KEYS = frozenset(
    {"host", "checked_at", "models", "indexed", "databases", "watchers", "schedules", "http", "backups", "broken"}
)


def _record_out(record: MemoryRecord) -> RecordOut:
    return RecordOut(
        id=record.id,
        kind=record.kind.value,
        topic=record.topic,
        status=record.status.value,
        content=record.content,
        source=record.source,
        confidence=record.confidence,
        occurred_at=record.occurred_at,
        created_at=record.created_at,
    )


def _appraisal_out(appraisal: Appraisal) -> AppraisalOut:
    return AppraisalOut(
        id=appraisal.record_id,
        rank=appraisal.rank,
        assessment=AssessmentOut(
            basis=appraisal.basis.value,
            sample=appraisal.sample,
            favourable=appraisal.favourable,
            unfavourable=appraisal.unfavourable,
            score=appraisal.score,
            rationale=appraisal.rationale,
            method=appraisal.method,
            rules=[
                RuleOut(
                    hypothesis_id=rule.hypothesis_id,
                    content=rule.content,
                    supporting=rule.supporting,
                    contradicting=rule.contradicting,
                    matched_on=list(rule.matched_on),
                )
                for rule in appraisal.rules
            ],
        ),
        binding=appraisal.binding,
        contract=CONTRACT,
    )


def _edge(decision_id: str, observation_id: str) -> MemoryLink:
    return MemoryLink(
        source_id=decision_id, target_id=observation_id, relation=MemoryRelation.DECIDED_FROM
    )


def _outcome_data(data: dict[str, Any], favourable: bool | None) -> dict[str, Any]:
    payload = dict(data)
    if favourable is not None:
        payload[FAVOURABLE_KEY] = favourable
    return payload


def authenticator_for(app_service: Any) -> ApiAuthenticator:
    config = getattr(app_service, "config", {}) or {}
    settings = AuthSettings.from_config(config.get("http") if isinstance(config, dict) else None)
    return ApiAuthenticator(
        secrets=getattr(app_service, "secrets", None),
        settings=settings,
        audit=getattr(app_service, "audit_stream", None),
    )


def create_app(app_service: Any, authenticator: ApiAuthenticator | None = None) -> FastAPI:
    api = FastAPI(title="Iris", version="1")
    appraiser = Appraiser(app_service.knowledge, app_service.knowledge_retriever)
    auth = authenticator or authenticator_for(app_service)
    api.state.authenticator = auth
    notice = auth.startup_notice()
    if notice:
        logger.warning("http surface unauthenticated", detail=notice)

    @api.middleware("http")
    async def _authenticate(request: Request, call_next: Any) -> Any:
        outcome = auth.authenticate(
            path=request.url.path,
            token=token_from_headers(request.headers),
            client_host=request.client.host if request.client else None,
        )
        if not outcome.ok:
            return JSONResponse(status_code=outcome.status_code, content={"detail": outcome.reason})
        request.state.client = outcome.client
        return await call_next(request)

    @api.exception_handler(KnowledgeError)
    async def _knowledge_error(_request: Any, error: KnowledgeError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(error)})

    @api.get("/health", response_model=Health)
    def health() -> Health:
        report = app_service.health() if callable(getattr(app_service, "health", None)) else {}
        return Health(
            status=str(report.get("status", "ok")),
            records=app_service.knowledge.records.count(),
            assistant=str(app_service.config.get("assistant_name", "Iris")),
            contract=CONTRACT,
            **{key: value for key, value in report.items() if key in HEALTH_DETAIL_KEYS},
        )

    @api.post("/observations", response_model=BatchAccepted, status_code=201)
    def observations(batch: ObservationBatch) -> BatchAccepted:
        topic = batch.topic.strip().lower()
        wanted = [
            (
                item,
                MemoryRecord(
                    kind=MemoryKind.OBSERVATION,
                    topic=(item.topic or topic).strip().lower(),
                    content=item.content,
                    source=item.source or batch.source,
                    data=item.data,
                    occurred_at=item.occurred_at,
                    source_ref=item.source_ref or batch.source_ref,
                ),
            )
            for item in batch.observations
        ]
        stored = app_service.knowledge.records.add_many([record for _, record in wanted])

        decisions: list[MemoryRecord] = []
        edges = []
        for (item, _), observation in zip(wanted, stored):
            if item.decision is None:
                continue
            decision = MemoryRecord(
                kind=MemoryKind.DECISION,
                topic=observation.topic,
                content=item.decision.content,
                source=item.decision.source or item.source or batch.source,
                data=item.decision.data,
                confidence=item.decision.score,
                occurred_at=item.occurred_at,
                source_ref=item.source_ref or batch.source_ref,
            )
            decisions.append(decision)
            edges.append((decision, observation.id))

        written_decisions: dict[str, str] = {}
        if decisions:
            app_service.knowledge.records.add_many(decisions)
            app_service.knowledge.links.add_many(
                [
                    _edge(decision.id, observation_id)
                    for decision, observation_id in edges
                ]
            )
            written_decisions = {observation_id: decision.id for decision, observation_id in edges}

        return BatchAccepted(
            written=len(stored),
            topic=topic,
            ids=[item.id for item in stored],
            records=[
                Written(id=item.id, decision_id=written_decisions.get(item.id)) for item in stored
            ],
        )

    @api.get("/observations/open", response_model=list[RecordOut])
    def open_observations(topic: str | None = None, limit: int = 50) -> list[RecordOut]:
        found = app_service.knowledge_review.open_observations(
            topic=topic.strip().lower() if topic else None, limit=max(1, min(int(limit), 500))
        )
        return [_record_out(item) for item in found]

    @api.post("/observations/{observation_id}/outcome", response_model=RecordOut, status_code=201)
    def close_observation(observation_id: str, outcome: OutcomeIn) -> RecordOut:
        record = app_service.knowledge.records.get(observation_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"No such observation: {observation_id}")
        if record.kind is not MemoryKind.OBSERVATION:
            raise HTTPException(
                status_code=400,
                detail=f"Outcomes close observations, and that is a {record.kind.value}",
            )
        stored = app_service.knowledge.record_outcomes(
            [
                (
                    observation_id,
                    MemoryRecord(
                        kind=MemoryKind.OUTCOME,
                        topic=record.topic,
                        content=outcome.content,
                        source=outcome.source,
                        data=_outcome_data(outcome.data, outcome.favourable),
                    ),
                )
            ]
        )
        return _record_out(stored[0])

    @api.post("/outcomes", response_model=dict, status_code=201)
    def close_many(batch: OutcomeBatch) -> dict:
        observations_by_id = {
            item.observation_id: app_service.knowledge.records.get(item.observation_id)
            for item in batch.outcomes
        }
        missing = [key for key, value in observations_by_id.items() if value is None]
        if missing:
            raise HTTPException(
                status_code=404, detail=f"No such observation: {', '.join(sorted(missing)[:5])}"
            )
        wrong_kind = [
            key
            for key, value in observations_by_id.items()
            if value is not None and value.kind is not MemoryKind.OBSERVATION
        ]
        if wrong_kind:
            raise HTTPException(
                status_code=400,
                detail=f"Outcomes close observations: {', '.join(sorted(wrong_kind)[:5])}",
            )

        stored = app_service.knowledge.record_outcomes(
            [
                (
                    item.observation_id,
                    MemoryRecord(
                        kind=MemoryKind.OUTCOME,
                        topic=observations_by_id[item.observation_id].topic,
                        content=item.content,
                        source=batch.source,
                        data=_outcome_data(item.data, item.favourable),
                    ),
                )
                for item in batch.outcomes
            ]
        )
        return {"written": len(stored), "ids": [item.id for item in stored]}

    @api.post("/assess", response_model=AssessOut)
    def assess(request: AssessIn) -> AssessOut:
        appraisals = appraiser.appraise_many(request.ids, record=request.record)
        return AssessOut(
            contract=CONTRACT,
            binding=False,
            ranked=sum(1 for item in appraisals if item.rank is not None),
            recorded=sum(1 for item in appraisals if request.record and item.topic),
            appraisals=[_appraisal_out(item) for item in appraisals],
        )

    @api.post("/compare", response_model=CompareOut)
    def compare(request: CompareIn) -> CompareOut:
        result = compare_rankers(app_service.knowledge, request.cohort, top_n=request.top_n)
        leader = result.leader
        return CompareOut(
            cohort=result.cohort,
            size=result.size,
            resolved=result.resolved,
            base_rate=result.base_rate,
            top_n=result.top_n,
            rankers=[
                RankerOut(
                    source=item.source,
                    scored=item.scored,
                    top_n=item.top_n,
                    hits=item.hits,
                    hit_rate=item.hit_rate,
                    lift=item.lift,
                    tied_at_the_cut=item.tied_at_the_cut,
                    distinct_scores=item.distinct_scores,
                )
                for item in result.rankers
            ],
            notes=result.notes,
            leader=leader.source if leader else None,
            summary=render_comparison(result),
        )

    @api.post("/recall", response_model=RecallOut)
    def recall(request: RecallIn) -> RecallOut:
        try:
            kinds = tuple(MemoryKind(value) for value in request.kinds)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        result = app_service.knowledge_retriever.retrieve(
            KnowledgeQuery(
                text=request.text,
                topic=request.topic.strip().lower() if request.topic else None,
                topic_prefix=request.topic_prefix,
                kinds=kinds,
                limit=request.limit,
                candidate_limit=request.candidate_limit,
                max_tokens=request.max_tokens,
            )
        )
        return RecallOut(
            records=[
                ScoredOut(record=_record_out(item.record), score=item.score, reasons=item.reasons)
                for item in result.records
            ],
            diagnostics=result.diagnostics,
        )

    return api
