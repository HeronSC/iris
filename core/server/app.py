# File: core/server/app.py

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from core.knowledge import KnowledgeError, KnowledgeQuery, MemoryKind, MemoryRecord
from core.knowledge.appraisal import CONTRACT, FAVOURABLE_KEY, Appraisal, Appraiser, verdict_for
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
    DecideIn,
    DecideOut,
    Health,
    ObservationBatch,
    ObservationIn,
    OutcomeBatch,
    OutcomeIn,
    OverlapOut,
    RankedOut,
    RankerOut,
    RankIn,
    RankOut,
    RecallIn,
    RecallOut,
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


@dataclass(frozen=True)
class DecideSettings:
    approve_at: float = 0.35
    veto_at: float = 0.15
    approve_lift: float | None = None
    veto_lift: float | None = None

    def thresholds(self, base_rate: float | None) -> tuple[float, float]:
        if (
            self.approve_lift is None
            or self.veto_lift is None
            or not isinstance(base_rate, (int, float))
            or base_rate <= 0
        ):
            return self.approve_at, self.veto_at
        approve_at = round(min(1.0, base_rate * self.approve_lift), 4)
        veto_at = round(base_rate * self.veto_lift, 4)
        if not 0.0 <= veto_at < approve_at <= 1.0:
            return self.approve_at, self.veto_at
        return approve_at, veto_at


def _trading_section(app_service: Any, name: str) -> dict[str, Any] | None:
    config = getattr(app_service, "config", None)
    trading = config.get("trading") if isinstance(config, dict) else None
    section = trading.get(name) if isinstance(trading, dict) else None
    return section if isinstance(section, dict) else None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def decide_settings(app_service: Any) -> DecideSettings:
    section = _trading_section(app_service, "decide")
    if section is None:
        return DecideSettings()
    try:
        approve_at = float(section.get("approve_at", DecideSettings.approve_at))
        veto_at = float(section.get("veto_at", DecideSettings.veto_at))
        approve_lift = _optional_float(section.get("approve_lift"))
        veto_lift = _optional_float(section.get("veto_lift"))
    except (TypeError, ValueError):
        return DecideSettings()
    if not 0.0 <= veto_at < approve_at <= 1.0:
        return DecideSettings()
    if (approve_lift is None) != (veto_lift is None) or (
        approve_lift is not None and not 0.0 < veto_lift < approve_lift
    ):
        approve_lift = veto_lift = None
    return DecideSettings(
        approve_at=approve_at, veto_at=veto_at, approve_lift=approve_lift, veto_lift=veto_lift
    )


def ignored_features_for(app_service: Any) -> tuple[str, ...]:
    section = _trading_section(app_service, "appraisal")
    names = section.get("ignore_features") if section else None
    if not isinstance(names, list):
        return ()
    return tuple(str(name) for name in names if isinstance(name, str) and name.strip())


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
    appraiser = Appraiser(
        app_service.knowledge,
        app_service.knowledge_retriever,
        ignored_features=ignored_features_for(app_service),
    )
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

    @api.post("/control/stop", response_model=Health)
    def control_stop() -> Health:
        halt = getattr(app_service, "halt", None)
        if callable(halt):
            halt()
        return health()

    @api.post("/control/resume", response_model=Health)
    def control_resume() -> Health:
        release = getattr(app_service, "release", None)
        if callable(release):
            release()
        return health()

    def _store_observations(
        topic: str, source: str, source_ref: str | None, items: list[ObservationIn]
    ) -> tuple[list[MemoryRecord], dict[str, str]]:
        wanted = [
            (
                item,
                MemoryRecord(
                    kind=MemoryKind.OBSERVATION,
                    topic=(item.topic or topic).strip().lower(),
                    content=item.content,
                    source=item.source or source,
                    data=item.data,
                    occurred_at=item.occurred_at,
                    source_ref=item.source_ref or source_ref,
                ),
            )
            for item in items
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
                source=item.decision.source or item.source or source,
                data=item.decision.data,
                confidence=item.decision.score,
                occurred_at=item.occurred_at,
                source_ref=item.source_ref or source_ref,
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
        return stored, written_decisions

    @api.post("/observations", response_model=BatchAccepted, status_code=201)
    def observations(batch: ObservationBatch) -> BatchAccepted:
        topic = batch.topic.strip().lower()
        stored, written_decisions = _store_observations(
            topic, batch.source, batch.source_ref, batch.observations
        )
        return BatchAccepted(
            written=len(stored),
            topic=topic,
            ids=[item.id for item in stored],
            records=[
                Written(id=item.id, decision_id=written_decisions.get(item.id)) for item in stored
            ],
        )

    @api.post("/decide", response_model=DecideOut, status_code=201)
    def decide(request: DecideIn) -> DecideOut:
        started = time.perf_counter()
        settings = decide_settings(app_service)
        topic = request.topic.strip().lower()
        item = ObservationIn(
            content=request.content,
            topic=topic,
            source=request.source,
            data=dict(request.features, symbol=request.symbol),
            occurred_at=request.occurred_at,
            source_ref=request.cohort,
            decision=request.bot,
        )
        stored, written_decisions = _store_observations(
            topic, request.source, request.cohort, [item]
        )
        observation = stored[0]
        appraisal = appraiser.appraise_many([observation.id])[0]
        approve_at, veto_at = settings.thresholds(appraisal.base_rate)
        verdict = verdict_for(appraisal, approve_at=approve_at, veto_at=veto_at)
        recorded = (
            appraiser.record(
                [appraisal],
                extra={
                    appraisal.record_id: {
                        "verdict": verdict,
                        "approve_at": approve_at,
                        "veto_at": veto_at,
                    }
                },
            )
            if request.record
            else []
        )
        logger.info(
            "decide",
            symbol=request.symbol,
            cohort=request.cohort,
            verdict=verdict,
            score=appraisal.score,
            sample=appraisal.sample,
            base_rate=appraisal.base_rate,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return DecideOut(
            observation_id=observation.id,
            bot_decision_id=written_decisions.get(observation.id),
            iris_decision_id=recorded[0].id if recorded else None,
            verdict=verdict,
            score=appraisal.score,
            sample=appraisal.sample,
            favourable=appraisal.favourable,
            unfavourable=appraisal.unfavourable,
            basis=appraisal.basis.value,
            method=appraisal.method,
            rationale=appraisal.rationale,
            approve_at=approve_at,
            veto_at=veto_at,
            base_rate=appraisal.base_rate,
            binding=False,
            contract=CONTRACT,
        )

    @api.post("/rank", response_model=RankOut, status_code=201)
    def rank(request: RankIn) -> RankOut:
        started = time.perf_counter()
        topic = request.topic.strip().lower()
        items = [
            ObservationIn(
                content=item.content or f"{item.symbol} in cohort {request.cohort}",
                topic=topic,
                source=request.source,
                data=dict(item.features, symbol=item.symbol),
                occurred_at=request.occurred_at,
                source_ref=request.cohort,
                decision=item.bot,
            )
            for item in request.items
        ]
        stored, bot_ids = _store_observations(topic, request.source, request.cohort, items)
        cohort = appraiser.appraise_cohort(
            [observation.id for observation in stored], features_only=True
        )
        by_id = {appraisal.record_id: appraisal for appraisal in cohort.appraisals}
        symbols = {observation.id: item.symbol for item, observation in zip(request.items, stored)}
        rates = {
            observation_id: (
                cohort.symbol_rate(symbol, request.shrink),
                cohort.symbol_days(symbol),
            )
            for observation_id, symbol in symbols.items()
        }
        iris_ids: dict[str, str] = {}
        if request.record:
            recorded = appraiser.record(
                cohort.appraisals,
                extra={
                    observation_id: {"symbol_rate": rate, "symbol_days": days}
                    for observation_id, (rate, days) in rates.items()
                },
            )
            wanted = [appraisal for appraisal in cohort.appraisals if appraisal.topic]
            iris_ids = {
                appraisal.record_id: decision.id for decision, appraisal in zip(recorded, wanted)
            }
        ordered = sorted(
            stored,
            key=lambda observation: (
                by_id[observation.id].score is None,
                -(by_id[observation.id].score or 0.0),
                -(rates[observation.id][0] or 0.0),
                symbols[observation.id],
            ),
        )
        ranked = [
            RankedOut(
                symbol=symbols[observation.id],
                observation_id=observation.id,
                rank=position,
                score=by_id[observation.id].score,
                sample=by_id[observation.id].sample,
                symbol_days=rates[observation.id][1],
                symbol_rate=rates[observation.id][0],
                bot_decision_id=bot_ids.get(observation.id),
                iris_decision_id=iris_ids.get(observation.id),
            )
            for position, observation in enumerate(ordered, start=1)
        ]
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        scored = sum(1 for item in ranked if item.score is not None)
        logger.info(
            "rank",
            topic=topic,
            cohort=request.cohort,
            size=len(ranked),
            scored=scored,
            base_rate=cohort.base_rate,
            elapsed_ms=elapsed_ms,
        )
        return RankOut(
            topic=topic,
            cohort=request.cohort,
            size=len(ranked),
            scored=scored,
            base_rate=cohort.base_rate,
            elapsed_ms=elapsed_ms,
            binding=False,
            contract=CONTRACT,
            ranked=ranked,
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
        result = compare_rankers(
            app_service.knowledge, request.cohort, top_n=request.top_n, label=request.label
        )
        leader = result.leader
        overlap = result.overlap
        return CompareOut(
            cohort=result.cohort,
            size=result.size,
            resolved=result.resolved,
            base_rate=result.base_rate,
            top_n=result.top_n,
            label=result.label,
            overlap=None
            if overlap is None
            else OverlapOut(
                first=overlap.first,
                second=overlap.second,
                both=overlap.both,
                first_only=overlap.first_only,
                second_only=overlap.second_only,
                neither=overlap.neither,
            ),
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
