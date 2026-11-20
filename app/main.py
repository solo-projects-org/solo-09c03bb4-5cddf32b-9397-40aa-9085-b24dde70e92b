"""Composition root and FastAPI app for the archive service."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException

from app.citations import CitationService, CitationValidation
from app.collector import Collector, DayBusy, RoundResult
from app.config import settings
from app.db import (
    Citation,
    CitationEvidence,
    CitationMismatch,
    Database,
    ReportAlreadyRegistered,
)
from app.schemas import (
    CitationOut,
    CitationRegisterIn,
    CitationRegisterOut,
    CitationTraceOut,
    CollectRequest,
    CollectResponse,
    DayCoverageOut,
    EvidenceOut,
    ManualRangeOut,
    NoticeOut,
    range_out,
)
from app.source_client import SourceClient
from common.times import parse_archive_date


class AppState:
    def __init__(
        self,
        db: Database,
        client: SourceClient,
        collector: Collector,
        citations: CitationService,
    ) -> None:
        self.db = db
        self.client = client
        self.collector = collector
        self.citations = citations


def build_state() -> AppState:
    db = Database(settings.db_path)
    db.init()
    client = SourceClient(settings.source_base_url, settings.source_timeout_seconds)
    collector = Collector(db, client, settings.max_round_queries)
    citations = CitationService(db)
    return AppState(
        db=db, client=client, collector=collector, citations=citations
    )


def set_state_for_tests(state: AppState) -> None:
    app.state.archive = state


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not getattr(app.state, "archive", None):
        app.state.archive = build_state()
    try:
        yield
    finally:
        state: AppState | None = getattr(app.state, "archive", None)
        if state is not None:
            state.client.close()


app = FastAPI(
    title="Environmental disclosure archive",
    version="1.1.0",
    description=(
        "Bounded continuation collection over a versioned disclosure source. "
        "Rounds verify the day version first, then search at most six minute "
        "intervals. Fewer than five results closes an interval; a full page "
        "splits it; a full single minute is left for manual review. "
        "Monthly-report citation lists can be registered against the submitted "
        "archive and later traced for revisions or withdrawals of cited notices."
    ),
    lifespan=lifespan,
)


def archive() -> AppState:
    state: AppState | None = getattr(app.state, "archive", None)
    if state is None:
        state = build_state()
        app.state.archive = state
    return state


def _parse_day(day: str) -> date:
    try:
        return parse_archive_date(day)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _round_payload(result: RoundResult) -> CollectResponse:
    return CollectResponse(
        day=result.day.isoformat(),
        status=result.status,
        day_version=result.day_version,
        queries_used=result.queries_used,
        version_reset=result.version_reset,
        collected_ranges=[
            range_out(r.start_minute, r.end_minute) for r in result.collected_ranges
        ],
        manual_ranges=[
            range_out(r.start_minute, r.end_minute) for r in result.manual_ranges
        ],
        pending_ranges=[
            range_out(r.start_minute, r.end_minute) for r in result.pending_ranges
        ],
        notices_collected=result.notices_collected,
        detail=result.detail,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/collection/rounds", response_model=CollectResponse)
def run_round(payload: CollectRequest) -> CollectResponse:
    day = _parse_day(payload.day)
    state = archive()
    max_queries = payload.max_queries or settings.max_round_queries
    try:
        result = state.collector.run_round(day, max_queries)
    except DayBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result.status == "source_error" and result.queries_used == 0 and result.detail:
        raise HTTPException(
            status_code=502, detail={"day": payload.day, "error": result.detail}
        )
    return _round_payload(result)


@app.get("/api/days/{day}", response_model=DayCoverageOut)
def day_coverage(day: str) -> DayCoverageOut:
    parsed = _parse_day(day)
    state = archive()
    record = state.db.get_day_record(parsed)
    completed, manual_merged = state.db.coverage(record)
    pending = state.db.pending_ranges(record)
    return DayCoverageOut(
        day=day,
        day_version=record.day_version,
        complete=not pending and not manual_merged,
        notices=[
            NoticeOut(
                announcement_no=n.announcement_no,
                title=n.title,
                published_at=n.published_at,
                published_minute=n.published_minute,
            )
            for n in record.notices
        ],
        completed_ranges=[range_out(r.start_minute, r.end_minute) for r in completed],
        pending_ranges=[range_out(r.start_minute, r.end_minute) for r in pending],
        manual_ranges=[
            ManualRangeOut(
                **range_out(r.start_minute, r.end_minute).model_dump(),
                reason=reason,
            )
            for r, reason in record.manual
        ],
    )


@app.get("/api/days/{day}/version")
def version_view(day: str) -> dict[str, str | None]:
    parsed = _parse_day(day)
    state = archive()
    record = state.db.get_day_record(parsed)
    return {"day": day, "day_version": record.day_version}


# --------------------------------------------------------------- citations


def _citation_out(citation: Citation) -> CitationOut:
    return CitationOut(
        day=citation.day.isoformat(),
        announcement_no=citation.announcement_no,
        title=citation.title,
        published_at=citation.published_at,
        day_version=citation.day_version,
        registered_at=citation.created_at,
    )


def _evidence_out(evidence: CitationEvidence) -> EvidenceOut:
    return EvidenceOut(
        day=evidence.day.isoformat(),
        announcement_no=evidence.announcement_no,
        reason=evidence.reason,
        day_version=evidence.day_version,
        new_title=evidence.new_title,
        new_published_at=evidence.new_published_at,
        detected_at=evidence.created_at,
    )


@app.post("/api/citation-registrations", response_model=CitationRegisterOut, status_code=201)
def register_citations(payload: CitationRegisterIn) -> CitationRegisterOut:
    state = archive()
    entries = [entry.model_dump() for entry in payload.citations]
    try:
        citations = state.citations.register(payload.report_no, entries)
    except CitationValidation as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ReportAlreadyRegistered as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CitationMismatch as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "citation list does not match the submitted archive",
                "errors": exc.errors,
            },
        ) from exc
    return CitationRegisterOut(
        report_no=payload.report_no,
        citations=[_citation_out(c) for c in citations],
    )


@app.get("/api/citation-registrations/{report_no}", response_model=CitationTraceOut)
def trace_citations(report_no: str) -> CitationTraceOut:
    state = archive()
    result = state.citations.trace(report_no)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"no citation registration for report {report_no}",
        )
    citations, evidence = result
    return CitationTraceOut(
        report_no=report_no,
        citations=[_citation_out(c) for c in citations],
        evidence=[_evidence_out(e) for e in evidence],
    )


__all__ = [
    "app",
    "AppState",
    "build_state",
    "set_state_for_tests",
]
