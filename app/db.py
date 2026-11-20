"""SQLite persistence for per-day coverage, frontier work and notices.

Two kinds of state are stored for a day:

* *settled* state: completed ranges, manual ranges and their notices;
* *frontier* state: the sub-intervals still to be searched.

Splitting a full page only creates frontier rows, so a round that runs out of
its query budget still makes durable progress and a restart continues from the
same frontier instead of re-walking the whole day.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from app.intervals import complement_intervals, merge_intervals
from app.models import Notice, RangeSpec
from common.versions import compare_versions


class VersionChanged(RuntimeError):
    """The day moved to a newer version while a write was in flight."""

    def __init__(self, day: str, new_version: str) -> None:
        super().__init__(f"{day} moved to newer day version {new_version}")
        self.day = day
        self.new_version = new_version


class StaleVersion(RuntimeError):
    """A write produced by an older day version was rejected."""

    def __init__(self, day: str, attempted: str, current: str) -> None:
        super().__init__(
            f"stale day version for {day}: attempted {attempted}, current {current}"
        )
        self.day = day
        self.attempted = attempted
        self.current = current


class DayMissing(RuntimeError):
    """Internal guard: a write referenced a day without a version row."""


class CitationMismatch(RuntimeError):
    """A registration list does not match the currently submitted archive.

    ``errors`` lists every offending citation so the API can reject the whole
    registration with a precise explanation.
    """

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class ReportAlreadyRegistered(RuntimeError):
    """A monthly report number was already registered."""

    def __init__(self, report_no: str) -> None:
        super().__init__(f"monthly report {report_no} is already registered")
        self.report_no = report_no


@dataclass(frozen=True)
class Citation:
    report_no: str
    day: date
    announcement_no: str
    title: str
    published_at: str
    day_version: str
    created_at: str


@dataclass(frozen=True)
class CitationEvidence:
    report_no: str
    day: date
    announcement_no: str
    reason: str
    day_version: str
    new_title: str | None
    new_published_at: str | None
    created_at: str


@dataclass
class FrontierItem:
    id: int
    start_minute: int
    end_minute: int

    @property
    def spec(self) -> RangeSpec:
        return RangeSpec(self.start_minute, self.end_minute)


@dataclass
class DayRecord:
    day: date
    day_version: str | None
    completed: list[RangeSpec] = field(default_factory=list)
    manual: list[tuple[RangeSpec, str]] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    frontier: list[FrontierItem] = field(default_factory=list)


SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    day TEXT PRIMARY KEY,
    day_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS completed_ranges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    start_minute INTEGER NOT NULL,
    end_minute INTEGER NOT NULL,
    day_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_completed_ranges_day
    ON completed_ranges(day);

CREATE TABLE IF NOT EXISTS manual_ranges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    start_minute INTEGER NOT NULL,
    end_minute INTEGER NOT NULL,
    reason TEXT NOT NULL,
    day_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_manual_ranges_day
    ON manual_ranges(day);

CREATE TABLE IF NOT EXISTS frontier_ranges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    start_minute INTEGER NOT NULL,
    end_minute INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(day, start_minute, end_minute)
);

CREATE INDEX IF NOT EXISTS idx_frontier_ranges_day
    ON frontier_ranges(day, start_minute);

CREATE TABLE IF NOT EXISTS notices (
    day TEXT NOT NULL,
    announcement_no TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT NOT NULL,
    published_minute INTEGER NOT NULL,
    day_version TEXT NOT NULL,
    PRIMARY KEY (day, announcement_no)
);

-- Citation registry: one row per cited notice of a monthly report.  A
-- registration is only accepted when every row matches the currently
-- submitted archive, and the day version seen at registration is kept.
CREATE TABLE IF NOT EXISTS citation_registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_no TEXT NOT NULL,
    day TEXT NOT NULL,
    announcement_no TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT NOT NULL,
    day_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(report_no, day, announcement_no)
);

CREATE INDEX IF NOT EXISTS idx_citations_day
    ON citation_registrations(day, announcement_no);

-- First confirmed change per citation.  The UNIQUE constraint enforces the
-- "keep only the first evidence" rule at the storage layer; restoring the
-- original text in a later version therefore cannot overwrite or delete it.
CREATE TABLE IF NOT EXISTS citation_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_no TEXT NOT NULL,
    day TEXT NOT NULL,
    announcement_no TEXT NOT NULL,
    reason TEXT NOT NULL,
    day_version TEXT NOT NULL,
    new_title TEXT,
    new_published_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(report_no, day, announcement_no)
);

CREATE INDEX IF NOT EXISTS idx_evidence_day
    ON citation_evidence(day, announcement_no);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Evidence reasons.  ``revised`` keeps the new title and published time;
# ``withdrawn`` means the same-number notice never reappeared once the new
# version's whole day had been collected.
REASON_REVISED = "revised"
REASON_WITHDRAWN = "withdrawn"


class Database:
    """One short-lived SQLite connection per operation keeps this thread-safe."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ------------------------------------------------------------------ reads

    def get_day_record(self, day: date) -> DayRecord:
        day_text = day.isoformat()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT day_version FROM days WHERE day = ?", (day_text,)
            ).fetchone()
            if row is None:
                return DayRecord(day=day, day_version=None)

            completed = [
                RangeSpec(r["start_minute"], r["end_minute"])
                for r in conn.execute(
                    "SELECT start_minute, end_minute FROM completed_ranges "
                    "WHERE day = ? ORDER BY start_minute, end_minute",
                    (day_text,),
                )
            ]
            manual = [
                (RangeSpec(r["start_minute"], r["end_minute"]), r["reason"])
                for r in conn.execute(
                    "SELECT start_minute, end_minute, reason FROM manual_ranges "
                    "WHERE day = ? ORDER BY start_minute, end_minute",
                    (day_text,),
                )
            ]
            frontier = [
                FrontierItem(
                    id=r["id"],
                    start_minute=r["start_minute"],
                    end_minute=r["end_minute"],
                )
                for r in conn.execute(
                    "SELECT id, start_minute, end_minute FROM frontier_ranges "
                    "WHERE day = ? ORDER BY start_minute, end_minute, id",
                    (day_text,),
                )
            ]
            notices = [
                Notice(
                    announcement_no=r["announcement_no"],
                    title=r["title"],
                    published_at=r["published_at"],
                    published_minute=r["published_minute"],
                )
                for r in conn.execute(
                    "SELECT announcement_no, title, published_at, published_minute "
                    "FROM notices WHERE day = ? "
                    "ORDER BY published_at, announcement_no",
                    (day_text,),
                )
            ]
        finally:
            conn.close()
        return DayRecord(
            day=day,
            day_version=row["day_version"],
            completed=completed,
            manual=manual,
            notices=notices,
            frontier=frontier,
        )

    # ------------------------------------------------------------- version gate

    def sync_version(self, day: date, source_version: str) -> str:
        """Reconcile the stored version with the source.

        Returns ``initialized``, ``reset`` or ``unchanged``.  A source that
        reports an older version than the archive raises ``StaleVersion`` so the
        archive never rolls backwards.
        """
        day_text = day.isoformat()
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT day_version FROM days WHERE day = ?", (day_text,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO days(day, day_version, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (day_text, source_version, now, now),
                )
                conn.commit()
                return "initialized"
            current = row["day_version"]
            comparison = compare_versions(source_version, current)
            if comparison < 0:
                raise StaleVersion(day_text, source_version, current)
            if comparison == 0:
                conn.commit()
                return "unchanged"
            self._reset_locked(conn, day_text, source_version, now)
            conn.commit()
            return "reset"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reset_to_version(self, day: date, new_version: str) -> bool:
        """Retract old announcements and coverage if ``new_version`` is newer."""
        day_text = day.isoformat()
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT day_version FROM days WHERE day = ?", (day_text,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO days(day, day_version, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (day_text, new_version, now, now),
                )
                conn.commit()
                return True
            current = row["day_version"]
            comparison = compare_versions(new_version, current)
            if comparison < 0:
                raise StaleVersion(day_text, new_version, current)
            if comparison == 0:
                conn.commit()
                return False
            self._reset_locked(conn, day_text, new_version, now)
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _reset_locked(
        conn: sqlite3.Connection, day_text: str, version: str, now: str
    ) -> None:
        conn.execute("DELETE FROM notices WHERE day = ?", (day_text,))
        conn.execute("DELETE FROM completed_ranges WHERE day = ?", (day_text,))
        conn.execute("DELETE FROM manual_ranges WHERE day = ?", (day_text,))
        conn.execute("DELETE FROM frontier_ranges WHERE day = ?", (day_text,))
        conn.execute(
            "UPDATE days SET day_version = ?, updated_at = ? WHERE day = ?",
            (version, now, day_text),
        )
        # Citation registrations and their evidence are intentionally kept:
        # the registry records what a monthly report cited, and confirmed
        # evidence must survive later versions even if the original text is
        # restored.  Re-collection re-evaluates every citation below.

    def _guard_version_locked(
        self, conn: sqlite3.Connection, day_text: str, expected: str
    ) -> None:
        """Reject or absorb a version race inside an open write transaction."""
        row = conn.execute(
            "SELECT day_version FROM days WHERE day = ?", (day_text,)
        ).fetchone()
        if row is None:
            raise DayMissing(f"day {day_text} has no version row")
        current = row["day_version"]
        comparison = compare_versions(expected, current)
        if comparison == 0:
            return
        if comparison < 0:
            raise StaleVersion(day_text, expected, current)
        # The worker learned about a newer version than the archive has.
        self._reset_locked(conn, day_text, expected, _utc_now())
        raise VersionChanged(day_text, expected)

    # -------------------------------------------------------------- frontier

    def seed_frontier(self, day: date) -> list[FrontierItem]:
        """Create frontier rows covering everything not yet settled.

        Idempotent: does nothing when a frontier already exists or the whole day
        is settled.
        """
        record = self.get_day_record(day)
        if record.day_version is None:
            raise DayMissing(f"day {day.isoformat()} has no version row")
        if record.frontier:
            return record.frontier
        occupied = [(r.start_minute, r.end_minute) for r in record.completed]
        occupied.extend((r.start_minute, r.end_minute) for r, _ in record.manual)
        gaps = complement_intervals(occupied)
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._guard_version_locked(conn, day.isoformat(), record.day_version)
            conn.execute("DELETE FROM frontier_ranges WHERE day = ?", (day.isoformat(),))
            conn.executemany(
                "INSERT OR IGNORE INTO frontier_ranges("
                "day, start_minute, end_minute, created_at) VALUES (?, ?, ?, ?)",
                [(day.isoformat(), start, end, now) for start, end in gaps],
            )
            conn.commit()
        except (VersionChanged, StaleVersion):
            conn.commit()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_day_record(day).frontier

    def _settle_locked(
        self,
        conn: sqlite3.Connection,
        day_text: str,
        expected_version: str,
        item: FrontierItem,
    ) -> None:
        self._guard_version_locked(conn, day_text, expected_version)
        deleted = conn.execute(
            "DELETE FROM frontier_ranges WHERE id = ? AND day = ?",
            (item.id, day_text),
        ).rowcount
        if deleted != 1:
            raise DayMissing(
                f"frontier item {item.id} vanished for {day_text}"
            )

    # ----------------------------------------------------------------- writes

    def complete_item(
        self,
        day: date,
        expected_version: str,
        item: FrontierItem,
        notices: list[Notice],
    ) -> None:
        """Close a frontier interval, storing its notices atomically.

        Citation evidence confirmed by this write is created inside the same
        transaction: a revision among the newly archived notices, or a
        withdrawal once the whole new version is settled.  Any failure rolls
        the archive change and the evidence back together.
        """
        day_text = day.isoformat()
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._settle_locked(conn, day_text, expected_version, item)
            for notice in notices:
                conn.execute(
                    "INSERT OR REPLACE INTO notices("
                    "day, announcement_no, title, published_at, "
                    "published_minute, day_version) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        day_text,
                        notice.announcement_no,
                        notice.title,
                        notice.published_at,
                        notice.published_minute,
                        expected_version,
                    ),
                )
                self._record_revision_locked(conn, day_text, notice, expected_version, now)
            conn.execute(
                "INSERT INTO completed_ranges("
                "day, start_minute, end_minute, day_version, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (day_text, item.start_minute, item.end_minute, expected_version, now),
            )
            self._record_withdrawals_locked(
                conn, day_text, expected_version, now, item
            )
            conn.commit()
        except (VersionChanged, StaleVersion):
            conn.commit()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def split_item(
        self,
        day: date,
        expected_version: str,
        item: FrontierItem,
        left: RangeSpec,
        right: RangeSpec,
    ) -> None:
        """Replace a full interval with two smaller frontier intervals."""
        day_text = day.isoformat()
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._settle_locked(conn, day_text, expected_version, item)
            conn.executemany(
                "INSERT OR IGNORE INTO frontier_ranges("
                "day, start_minute, end_minute, created_at) VALUES (?, ?, ?, ?)",
                [
                    (day_text, left.start_minute, left.end_minute, now),
                    (day_text, right.start_minute, right.end_minute, now),
                ],
            )
            conn.commit()
        except (VersionChanged, StaleVersion):
            conn.commit()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def manual_item(
        self,
        day: date,
        expected_version: str,
        item: FrontierItem,
        reason: str,
    ) -> None:
        """Move a single-minute interval that stayed full to manual review."""
        day_text = day.isoformat()
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._settle_locked(conn, day_text, expected_version, item)
            conn.execute(
                "INSERT INTO manual_ranges("
                "day, start_minute, end_minute, reason, day_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    day_text,
                    item.start_minute,
                    item.end_minute,
                    reason,
                    expected_version,
                    now,
                ),
            )
            conn.commit()
        except (VersionChanged, StaleVersion):
            conn.commit()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ views

    def coverage(self, record: DayRecord) -> tuple[list[RangeSpec], list[RangeSpec]]:
        covered = merge_intervals(
            [(r.start_minute, r.end_minute) for r in record.completed]
        )
        manual = merge_intervals(
            [(r.start_minute, r.end_minute) for r, _ in record.manual]
        )
        return (
            [RangeSpec(start, end) for start, end in covered],
            [RangeSpec(start, end) for start, end in manual],
        )

    def pending_ranges(self, record: DayRecord) -> list[RangeSpec]:
        """Minute intervals that no completed or manual range covers.

        The persisted frontier is always the complement of the settled ranges,
        so either representation yields the same merged answer; the complement
        is used directly because it also works before the frontier is seeded.
        """
        occupied = [(r.start_minute, r.end_minute) for r in record.completed]
        occupied.extend((r.start_minute, r.end_minute) for r, _ in record.manual)
        return [
            RangeSpec(start, end) for start, end in complement_intervals(occupied)
        ]

    # ------------------------------------------------------------- citations

    def register_citations(
        self, report_no: str, entries: list[dict[str, str]]
    ) -> list[Citation]:
        """Register one monthly report's citation list atomically.

        The whole list must match the currently submitted archive: for every
        entry the day must be initialised and a notice with the same
        announcement number, title and ``published_at`` must be archived.  On
        any mismatch nothing is written and :class:`CitationMismatch` lists
        every offending citation.  A second registration of the same report
        number is rejected as :class:`ReportAlreadyRegistered`.
        """
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT 1 FROM citation_registrations WHERE report_no = ? LIMIT 1",
                (report_no,),
            ).fetchone()
            if existing is not None:
                raise ReportAlreadyRegistered(report_no)

            errors: list[str] = []
            rows: list[tuple[str, str, str, str, str, str, str]] = []
            for index, entry in enumerate(entries):
                day_text = entry["day"]
                announcement_no = entry["announcement_no"]
                title = entry["title"]
                published_at = entry["published_at"]
                day_row = conn.execute(
                    "SELECT day_version FROM days WHERE day = ?", (day_text,)
                ).fetchone()
                if day_row is None:
                    errors.append(
                        f"citation #{index + 1} ({announcement_no} on {day_text}): "
                        "that day has no submitted archive yet"
                    )
                    continue
                notice = conn.execute(
                    "SELECT title, published_at FROM notices "
                    "WHERE day = ? AND announcement_no = ?",
                    (day_text, announcement_no),
                ).fetchone()
                if notice is None:
                    errors.append(
                        f"citation #{index + 1} ({announcement_no} on {day_text}): "
                        "announcement is not present in the submitted archive"
                    )
                elif notice["title"] != title or notice["published_at"] != published_at:
                    errors.append(
                        f"citation #{index + 1} ({announcement_no} on {day_text}): "
                        "title/published_at differ from the submitted archive"
                    )
                else:
                    rows.append(
                        (
                            report_no,
                            day_text,
                            announcement_no,
                            title,
                            published_at,
                            day_row["day_version"],
                            now,
                        )
                    )
            if errors:
                raise CitationMismatch(errors)

            conn.executemany(
                "INSERT INTO citation_registrations("
                "report_no, day, announcement_no, title, published_at, "
                "day_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        except (ReportAlreadyRegistered, CitationMismatch):
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_citations(report_no)[0]

    def get_citations(
        self, report_no: str
    ) -> tuple[list[Citation], list[CitationEvidence]] | None:
        """Return a report's original citations and confirmed evidence.

        Returns ``None`` when the report number was never registered.
        """
        conn = self._connect()
        try:
            header = conn.execute(
                "SELECT 1 FROM citation_registrations WHERE report_no = ? LIMIT 1",
                (report_no,),
            ).fetchone()
            if header is None:
                return None
            citations = [
                Citation(
                    report_no=r["report_no"],
                    day=date.fromisoformat(r["day"]),
                    announcement_no=r["announcement_no"],
                    title=r["title"],
                    published_at=r["published_at"],
                    day_version=r["day_version"],
                    created_at=r["created_at"],
                )
                for r in conn.execute(
                    "SELECT report_no, day, announcement_no, title, published_at, "
                    "day_version, created_at FROM citation_registrations "
                    "WHERE report_no = ? ORDER BY day, announcement_no, id",
                    (report_no,),
                )
            ]
            evidence = [
                CitationEvidence(
                    report_no=r["report_no"],
                    day=date.fromisoformat(r["day"]),
                    announcement_no=r["announcement_no"],
                    reason=r["reason"],
                    day_version=r["day_version"],
                    new_title=r["new_title"],
                    new_published_at=r["new_published_at"],
                    created_at=r["created_at"],
                )
                for r in conn.execute(
                    "SELECT report_no, day, announcement_no, reason, day_version, "
                    "new_title, new_published_at, created_at FROM citation_evidence "
                    "WHERE report_no = ? ORDER BY day, announcement_no, id",
                    (report_no,),
                )
            ]
        finally:
            conn.close()
        return citations, evidence

    def _record_revision_locked(
        self,
        conn: sqlite3.Connection,
        day_text: str,
        notice: Notice,
        version: str,
        now: str,
    ) -> None:
        """Record the first revision of a cited same-day same-number notice.

        Only citations without evidence yet are considered, so later versions
        (including one that restores the original title/time) never overwrite
        or remove the first confirmation.
        """
        matches = conn.execute(
            "SELECT c.report_no FROM citation_registrations c "
            "WHERE c.day = ? AND c.announcement_no = ? "
            "AND (c.title <> ? OR c.published_at <> ?) "
            "AND NOT EXISTS (SELECT 1 FROM citation_evidence e "
            "WHERE e.report_no = c.report_no AND e.day = c.day "
            "AND e.announcement_no = c.announcement_no)",
            (day_text, notice.announcement_no, notice.title, notice.published_at),
        ).fetchall()
        for row in matches:
            conn.execute(
                "INSERT INTO citation_evidence("
                "report_no, day, announcement_no, reason, day_version, "
                "new_title, new_published_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["report_no"],
                    day_text,
                    notice.announcement_no,
                    REASON_REVISED,
                    version,
                    notice.title,
                    notice.published_at,
                    now,
                ),
            )

    def _record_withdrawals_locked(
        self,
        conn: sqlite3.Connection,
        day_text: str,
        version: str,
        now: str,
        item: FrontierItem,
    ) -> None:
        """Record withdrawals once the new version's whole day is settled.

        A missing same-number notice is only evidence after the day has no
        frontier left and no manual ranges, i.e. the full day was collected.
        Citations already carrying evidence (an earlier revision, say) keep
        their first confirmation and are not reclassified.
        """
        unsettled = conn.execute(
            "SELECT 1 FROM frontier_ranges WHERE day = ? LIMIT 1", (day_text,)
        ).fetchone()
        if unsettled is not None:
            return
        manual = conn.execute(
            "SELECT 1 FROM manual_ranges WHERE day = ? LIMIT 1", (day_text,)
        ).fetchone()
        if manual is not None:
            return
        missing = conn.execute(
            "SELECT c.report_no, c.announcement_no FROM citation_registrations c "
            "WHERE c.day = ? AND NOT EXISTS (SELECT 1 FROM notices n "
            "WHERE n.day = c.day AND n.announcement_no = c.announcement_no) "
            "AND NOT EXISTS (SELECT 1 FROM citation_evidence e "
            "WHERE e.report_no = c.report_no AND e.day = c.day "
            "AND e.announcement_no = c.announcement_no)",
            (day_text,),
        ).fetchall()
        for row in missing:
            conn.execute(
                "INSERT INTO citation_evidence("
                "report_no, day, announcement_no, reason, day_version, "
                "new_title, new_published_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)",
                (
                    row["report_no"],
                    day_text,
                    row["announcement_no"],
                    REASON_WITHDRAWN,
                    version,
                    now,
                ),
            )
