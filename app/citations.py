"""Citation registry service.

Bridges the HTTP schemas and the SQLite-backed :class:`~app.db.Database`.

Registration accepts a monthly report number plus a citation list.  Every
citation carries its UTC disclosure day, announcement number, title and
published time, and the whole list must match one currently submitted
archive snapshot before anything is stored.  Archive-driven changes
(revision / withdrawal) are produced by the collector's write path in
``app.db`` and only read back here.
"""

from __future__ import annotations

from app.db import (
    Citation,
    CitationEvidence,
    Database,
)
from common.times import parse_archive_date, parse_published_at


class CitationValidation(ValueError):
    """The citation payload itself is malformed (independent of the archive)."""


def _validate_entries(raw_entries: list[dict[str, str]]) -> list[dict[str, str]]:
    """Normalise and validate a citation list without touching the archive."""
    if not raw_entries:
        raise CitationValidation("citations must not be an empty list")
    normalised: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(raw_entries):
        prefix = f"citation #{index + 1}"
        day_text = str(entry.get("day", "")).strip()
        announcement_no = str(entry.get("announcement_no", "")).strip()
        title = str(entry.get("title", "")).strip()
        published_at = str(entry.get("published_at", "")).strip()
        if not day_text:
            raise CitationValidation(f"{prefix}: day is required")
        if not announcement_no:
            raise CitationValidation(f"{prefix}: announcement_no is required")
        if not title:
            raise CitationValidation(f"{prefix}: title is required")
        if not published_at:
            raise CitationValidation(f"{prefix}: published_at is required")
        try:
            day = parse_archive_date(day_text)
        except ValueError as exc:
            raise CitationValidation(f"{prefix}: {exc}") from exc
        try:
            normalised_at, _ = parse_published_at(published_at, day)
        except ValueError as exc:
            raise CitationValidation(f"{prefix}: {exc}") from exc
        key = (day_text, announcement_no)
        if key in seen:
            raise CitationValidation(
                f"{prefix}: duplicate citation for {announcement_no} on {day_text}"
            )
        seen.add(key)
        normalised.append(
            {
                "day": day_text,
                "announcement_no": announcement_no,
                "title": title,
                "published_at": normalised_at,
            }
        )
    return normalised


class CitationService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def register(
        self, report_no: str, raw_entries: list[dict[str, str]]
    ) -> list[Citation]:
        report_no = report_no.strip()
        if not report_no:
            raise CitationValidation("report_no is required")
        entries = _validate_entries(raw_entries)
        return self.db.register_citations(report_no, entries)

    def trace(
        self, report_no: str
    ) -> tuple[list[Citation], list[CitationEvidence]] | None:
        return self.db.get_citations(report_no.strip())
