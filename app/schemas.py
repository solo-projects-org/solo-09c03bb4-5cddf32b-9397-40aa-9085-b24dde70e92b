"""HTTP schemas for the archive service."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from common.times import minute_label


class RangeOut(BaseModel):
    start_minute: int
    end_minute: int
    from_time: str
    to_time: str


class ManualRangeOut(RangeOut):
    reason: str


class NoticeOut(BaseModel):
    announcement_no: str
    title: str
    published_at: str
    published_minute: int


class CollectRequest(BaseModel):
    day: str = Field(
        description="Archive day in YYYY-MM-DD (UTC)",
        examples=["2026-11-20"],
    )
    max_queries: int | None = Field(
        default=None,
        ge=1,
        le=6,
        description="Queries allowed in this round; defaults to the service cap of 6",
        examples=[6],
    )

    model_config = {
        "json_schema_extra": {"example": {"day": "2026-11-20", "max_queries": 6}}
    }


class CollectResponse(BaseModel):
    day: str
    status: str
    day_version: str | None
    queries_used: int
    version_reset: bool
    collected_ranges: list[RangeOut]
    manual_ranges: list[RangeOut]
    pending_ranges: list[RangeOut]
    notices_collected: int
    detail: str | None = None


class DayCoverageOut(BaseModel):
    day: str
    day_version: str | None
    complete: bool
    notices: list[NoticeOut]
    completed_ranges: list[RangeOut]
    pending_ranges: list[RangeOut]
    manual_ranges: list[ManualRangeOut]


def range_out(start: int, end: int) -> RangeOut:
    return RangeOut(
        start_minute=start,
        end_minute=end,
        from_time=minute_label(start),
        to_time=minute_label(end),
    )


# --------------------------------------------------------------- citations


class CitationIn(BaseModel):
    day: str = Field(
        description="UTC disclosure day of the cited notice, YYYY-MM-DD",
        examples=["2026-11-19"],
    )
    announcement_no: str = Field(
        min_length=1,
        description="Announcement number as shown on the disclosure source",
        examples=["HB-EIA-2026-1119-01"],
    )
    title: str = Field(
        min_length=1, description="Title recorded in the monthly report"
    )
    published_at: str = Field(
        min_length=1,
        description="UTC publication time as an ISO-8601 timestamp",
        examples=["2026-11-19T08:00:00Z"],
    )


class CitationRegisterIn(BaseModel):
    report_no: str = Field(
        min_length=1,
        description="Monthly report number; one registration per report",
        examples=["EIA-MONTHLY-2026-11"],
    )
    citations: list[CitationIn] = Field(
        min_length=1, description="Full citation list of the monthly report"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "report_no": "EIA-MONTHLY-2026-11",
                "citations": [
                    {
                        "day": "2026-11-19",
                        "announcement_no": "HB-EIA-2026-1119-01",
                        "title": "云岭风电场二期工程环境影响报告书公示",
                        "published_at": "2026-11-19T08:00:00Z",
                    },
                    {
                        "day": "2026-11-19",
                        "announcement_no": "HB-EIA-2026-1119-02",
                        "title": "青石镇污水处理厂提标改造竣工验收公示",
                        "published_at": "2026-11-19T12:00:00Z",
                    },
                ],
            }
        }
    }


class CitationOut(BaseModel):
    day: str
    announcement_no: str
    title: str
    published_at: str
    day_version: str = Field(description="Day version retained at registration")
    registered_at: str


class EvidenceOut(BaseModel):
    day: str
    announcement_no: str
    reason: Literal["revised", "withdrawn"]
    day_version: str = Field(description="Day version in which the change was confirmed")
    new_title: str | None = None
    new_published_at: str | None = None
    detected_at: str


class CitationRegisterOut(BaseModel):
    report_no: str
    citations: list[CitationOut]


class CitationTraceOut(BaseModel):
    report_no: str
    citations: list[CitationOut]
    evidence: list[EvidenceOut]
