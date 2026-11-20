"""Citation registry: registration, revision, withdrawal, first-evidence rule.

These tests drive the real collector over the local dataset store and the
real SQLite layer, so they also cover the "archive change and evidence land
in one transaction" guarantee.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.citations import CitationService, CitationValidation
from app.db import (
    CitationMismatch,
    Database,
    REASON_REVISED,
    REASON_WITHDRAWN,
    ReportAlreadyRegistered,
)
from app.collector import Collector
from source.store import SourceState
from tests.conftest import DATASETS, StubSource, collect_until_settled

REPORT = "EIA-MONTHLY-2026-11"
DAY_19 = date(2026, 11, 19)
DAY_21 = date(2026, 11, 21)


def entry(day: str, no: str, title: str, at: str) -> dict[str, str]:
    return {
        "day": day,
        "announcement_no": no,
        "title": title,
        "published_at": at,
    }


V1_CITATIONS = [
    entry(
        "2026-11-19",
        "HB-EIA-2026-1119-01",
        "云岭风电场二期工程环境影响报告书公示",
        "2026-11-19T08:00:00Z",
    ),
    entry(
        "2026-11-19",
        "HB-EIA-2026-1119-02",
        "青石镇污水处理厂提标改造竣工验收公示",
        "2026-11-19T12:00:00Z",
    ),
    entry(
        "2026-11-21",
        "HB-EIA-2026-1121-05",
        "桃李乡养殖场粪污处理设施公示",
        "2026-11-21T20:00:00Z",
    ),
]


@pytest.fixture()
def service(db: Database) -> CitationService:
    return CitationService(db)


def test_registration_requires_a_matching_submitted_archive(
    db: Database, collector: Collector, service: CitationService
) -> None:
    # Nothing collected yet: the archive has no submitted snapshot for the day.
    with pytest.raises(CitationMismatch) as exc:
        service.register(REPORT, V1_CITATIONS)
    assert len(exc.value.errors) == 3

    collect_until_settled(collector, DAY_19)
    # Day 21 still missing, so the whole list must still be rejected.
    with pytest.raises(CitationMismatch) as exc:
        service.register(REPORT, V1_CITATIONS)
    assert any("2026-11-21" in error for error in exc.value.errors)
    assert service.trace(REPORT) is None  # nothing was stored

    collect_until_settled(collector, DAY_21)
    citations = service.register(REPORT, V1_CITATIONS)
    assert [c.day_version for c in citations] == [
        "2026-11-19.v1",
        "2026-11-19.v1",
        "2026-11-21.v1",
    ]


def test_registration_rejects_title_and_time_differences(
    db: Database, collector: Collector, service: CitationService
) -> None:
    collect_until_settled(collector, DAY_19)

    wrong_title = [
        entry(
            "2026-11-19",
            "HB-EIA-2026-1119-01",
            "不存在的标题",
            "2026-11-19T08:00:00Z",
        )
    ]
    with pytest.raises(CitationMismatch) as exc:
        service.register(REPORT, wrong_title)
    assert "differ" in exc.value.errors[0]

    wrong_time = [
        entry(
            "2026-11-19",
            "HB-EIA-2026-1119-01",
            "云岭风电场二期工程环境影响报告书公示",
            "2026-11-19T09:00:00Z",
        )
    ]
    with pytest.raises(CitationMismatch):
        service.register(REPORT, wrong_time)

    missing = [
        entry("2026-11-19", "HB-EIA-2026-9999-99", "x", "2026-11-19T08:00:00Z")
    ]
    with pytest.raises(CitationMismatch) as exc:
        service.register(REPORT, missing)
    assert "not present" in exc.value.errors[0]
    assert service.trace(REPORT) is None


def test_payload_validation_rules(service: CitationService) -> None:
    with pytest.raises(CitationValidation):
        service.register(REPORT, [])
    with pytest.raises(CitationValidation):
        service.register("  ", V1_CITATIONS)
    with pytest.raises(CitationValidation):
        service.register(
            REPORT,
            [
                {
                    "day": "2026/11/19",
                    "announcement_no": "X",
                    "title": "t",
                    "published_at": "2026-11-19T08:00:00Z",
                }
            ],
        )
    duplicate = [
        V1_CITATIONS[0],
        entry(
            "2026-11-19",
            "HB-EIA-2026-1119-01",
            "云岭风电场二期工程环境影响报告书公示",
            "2026-11-19T08:00:00Z",
        ),
    ]
    with pytest.raises(CitationValidation):
        service.register(REPORT, duplicate)


def test_second_registration_of_same_report_is_rejected(
    db: Database, collector: Collector, service: CitationService
) -> None:
    collect_until_settled(collector, DAY_19)
    payload = V1_CITATIONS[:2]
    service.register(REPORT, payload)
    with pytest.raises(ReportAlreadyRegistered):
        service.register(REPORT, payload)


def test_revision_is_recorded_when_new_version_enters_archive(
    db: Database, collector: Collector, stub: StubSource, service: CitationService
) -> None:
    collect_until_settled(collector, DAY_19)
    service.register(REPORT, V1_CITATIONS[:2])

    stub.switch_version_sync("v2")
    collect_until_settled(collector, DAY_19)

    citations, evidence = service.trace(REPORT)
    assert len(citations) == 2  # original citation text is immutable
    assert len(evidence) == 1
    changed = evidence[0]
    assert changed.announcement_no == "HB-EIA-2026-1119-01"
    assert changed.reason == REASON_REVISED
    assert changed.day_version == "2026-11-19.v2"
    assert changed.new_title == "云岭风电场二期工程环境影响报告书公示（修订版）"
    assert changed.new_published_at == "2026-11-19T08:00:00Z"


def test_published_time_change_is_also_a_revision(
    db: Database, collector: Collector, stub: StubSource, service: CitationService
) -> None:
    collect_until_settled(collector, DAY_19)
    service.register(REPORT, V1_CITATIONS[:2])

    stub.switch_version_sync("v2")
    collect_until_settled(collector, DAY_19)
    stub.switch_version_sync("v3")
    collect_until_settled(collector, DAY_19)

    _, evidence = service.trace(REPORT)
    by_no = {e.announcement_no: e for e in evidence}
    # -02 keeps its v1 title in v3 but moves from 12:00 to 12:30.
    moved = by_no["HB-EIA-2026-1119-02"]
    assert moved.reason == REASON_REVISED
    assert moved.day_version == "2026-11-19.v3"
    assert moved.new_published_at == "2026-11-19T12:30:00Z"
    assert moved.new_title == "青石镇污水处理厂提标改造竣工验收公示"


def test_withdrawal_only_after_the_whole_new_version_is_collected(
    db: Database, collector: Collector, stub: StubSource, service: CitationService
) -> None:
    collect_until_settled(collector, DAY_21)
    service.register(REPORT, [V1_CITATIONS[2]])

    # v3 drops notice -05.  The day now holds five notices, so the first query
    # returns a full page and only splits the interval: no settlement yet.
    stub.switch_version_sync("v3")
    first = collector.run_round(DAY_21, max_queries=1)
    assert first.status == "partial"
    _, evidence = service.trace(REPORT)
    assert evidence == []  # not yet: the new version is not fully collected

    # While any frontier interval remains, the missing notice is not evidence.
    settled = False
    for _ in range(15):
        result = collector.run_round(DAY_21, max_queries=1)
        _, evidence = service.trace(REPORT)
        if result.status == "collected":
            settled = True
            assert len(evidence) == 1
            break
        assert evidence == []
    assert settled

    withdrawn = evidence[0]
    assert withdrawn.reason == REASON_WITHDRAWN
    assert withdrawn.announcement_no == "HB-EIA-2026-1121-05"
    assert withdrawn.day_version == "2026-11-21.v3"
    assert withdrawn.new_title is None
    assert withdrawn.new_published_at is None
    record = db.get_day_record(DAY_21)
    assert db.pending_ranges(record) == []


def test_withdrawal_waits_for_settlement_on_a_dense_day(
    db: Database, service: CitationService
) -> None:
    """A missing notice on a partially collected day must not be 'withdrawn'."""
    state = SourceState(DATASETS, "v1")
    collector = Collector(db, StubSource(state))
    day = date(2026, 11, 20)
    # Sparse minutes settle first; -05 lives in the 09:12 manual cluster and is
    # never archived in v1, so cite a notice that *is* archived: -06 at 10:05.
    collect_until_settled(collector, day)
    target = entry(
        "2026-11-20",
        "HB-EIA-2026-1120-06",
        "东岭矿区生态修复方案公示",
        "2026-11-20T10:05:00Z",
    )
    service.register(REPORT, [target])

    # Custom v3-like state where -06 is absent and the day stays split.
    asyncio.run(state.switch_version("v2"))
    first = collector.run_round(day, max_queries=1)
    assert first.status == "partial"
    _, evidence = service.trace(REPORT)
    assert evidence == []  # not yet: the new version is not fully collected

    collect_until_settled(collector, day)
    # v2 still contains -06, so there must be no evidence at all.
    _, evidence = service.trace(REPORT)
    assert evidence == []


def test_first_evidence_is_kept_when_originals_are_restored(
    db: Database, collector: Collector, stub: StubSource, service: CitationService
) -> None:
    collect_until_settled(collector, DAY_19)
    collect_until_settled(collector, DAY_21)
    service.register(REPORT, V1_CITATIONS)

    for version in ("v2", "v3"):
        stub.switch_version_sync(version)
        collect_until_settled(collector, DAY_19)
        collect_until_settled(collector, DAY_21)

    _, evidence_v3 = service.trace(REPORT)
    assert {e.reason for e in evidence_v3} == {REASON_REVISED, REASON_WITHDRAWN}
    snapshot = [(e.announcement_no, e.reason, e.day_version) for e in evidence_v3]

    # v4 restores the original titles/times and republishes the withdrawn one.
    stub.switch_version_sync("v4")
    collect_until_settled(collector, DAY_19)
    collect_until_settled(collector, DAY_21)

    citations, evidence_v4 = service.trace(REPORT)
    assert len(citations) == 3
    assert [
        (e.announcement_no, e.reason, e.day_version) for e in evidence_v4
    ] == snapshot  # first confirmations are immutable
    record_19 = db.get_day_record(DAY_19)
    titles = {n.announcement_no: n.title for n in record_19.notices}
    assert titles["HB-EIA-2026-1119-01"] == "云岭风电场二期工程环境影响报告书公示"


def test_registry_and_evidence_survive_version_reset(
    db: Database, collector: Collector, stub: StubSource, service: CitationService
) -> None:
    collect_until_settled(collector, DAY_19)
    service.register(REPORT, V1_CITATIONS[:2])
    stub.switch_version_sync("v2")
    collect_until_settled(collector, DAY_19)

    citations, evidence = service.trace(REPORT)
    assert len(citations) == 2 and len(evidence) == 1


def test_evidence_insert_failure_rolls_back_the_archive_write(
    db: Database, collector: Collector, stub: StubSource, service: CitationService
) -> None:
    collect_until_settled(collector, DAY_19)
    service.register(REPORT, V1_CITATIONS[:2])

    calls = {"n": 0}
    real_record = db._record_revision_locked

    def fail_on_evidence(conn, day_text, notice, version, now):
        calls["n"] += 1
        raise RuntimeError("simulated evidence write failure")

    db._record_revision_locked = fail_on_evidence  # type: ignore[method-assign]
    stub.switch_version_sync("v2")
    with pytest.raises(RuntimeError, match="simulated evidence"):
        collector.run_round(DAY_19)
    assert calls["n"] >= 1

    db._record_revision_locked = real_record  # type: ignore[method-assign]
    # The complete_item transaction rolled back together with the evidence:
    # no v2 notices, no completed range and the frontier item are all back.
    record = db.get_day_record(DAY_19)
    assert record.day_version == "2026-11-19.v2"  # pre-flight reset already committed
    assert record.notices == []
    assert record.completed == []
    assert record.frontier, "the settled frontier item must be restored by rollback"
    _, evidence = service.trace(REPORT)
    assert evidence == []

    # Retrying the round now succeeds and records the evidence normally.
    collect_until_settled(collector, DAY_19)
    record = db.get_day_record(DAY_19)
    assert record.day_version == "2026-11-19.v2"
    titles = {n.announcement_no: n.title for n in record.notices}
    assert titles["HB-EIA-2026-1119-01"] == "云岭风电场二期工程环境影响报告书公示（修订版）"
    _, evidence = service.trace(REPORT)
    assert len(evidence) == 1 and evidence[0].reason == REASON_REVISED


def test_registration_mismatch_leaves_no_partial_rows(
    db: Database, collector: Collector, service: CitationService
) -> None:
    collect_until_settled(collector, DAY_19)
    collect_until_settled(collector, DAY_21)
    bad = V1_CITATIONS + [
        entry("2026-11-19", "HB-EIA-2026-0000-00", "幽灵公告", "2026-11-19T00:00:00Z")
    ]
    with pytest.raises(CitationMismatch):
        service.register(REPORT, bad)
    assert service.trace(REPORT) is None


def test_registry_survives_restart(
    tmp_path, collector: Collector, service: CitationService, db: Database
) -> None:
    collect_until_settled(collector, DAY_19)
    service.register(REPORT, V1_CITATIONS[:2])

    restarted_db = Database(db.path)
    restarted_db.init()
    restarted = CitationService(restarted_db)
    citations, evidence = restarted.trace(REPORT)
    assert len(citations) == 2
    assert evidence == []
    assert citations[0].title == V1_CITATIONS[0]["title"]
