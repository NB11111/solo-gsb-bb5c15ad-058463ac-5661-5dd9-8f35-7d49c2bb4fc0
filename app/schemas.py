"""Pydantic request/response models for the audit API."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

MICROSECOND_SCALE = 6


class Event(BaseModel):
    id: str = Field(..., min_length=1, description="Unique event identifier")
    earliest: datetime = Field(
        ..., description="Earliest possible time (RFC 3339, explicit UTC offset)"
    )
    latest: datetime = Field(
        ..., description="Latest possible time (RFC 3339, explicit UTC offset)"
    )

    @field_validator("earliest", "latest")
    @classmethod
    def _must_be_offset_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "timestamp must be RFC 3339 with an explicit offset, e.g. '2026-09-21T10:00:00Z'"
            )
        return value


class GapConstraint(BaseModel):
    type: Literal["at_least", "at_most"] = Field(
        ..., description="at_least: right >= left + gap; at_most: right <= left + gap"
    )
    left_event: str = Field(..., min_length=1, description="ID of the earlier-side event")
    right_event: str = Field(..., min_length=1, description="ID of the other event")
    seconds: Decimal = Field(
        ...,
        ge=0,
        description="Gap size in seconds; non-negative, at most microsecond resolution",
    )
    id: Optional[str] = Field(
        default=None, min_length=1, description="Optional constraint identifier for evidence"
    )

    @field_validator("seconds")
    @classmethod
    def _whole_microseconds(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("seconds must be non-negative")
        # format(..., "f") avoids scientific notation for very small values
        fractional = format(value, "f").split(".", 1)
        if len(fractional) == 2 and len(fractional[1].rstrip("0")) > MICROSECOND_SCALE:
            raise ValueError("seconds may not have finer than 1e-6 (microsecond) resolution")
        return value


class AuditRequest(BaseModel):
    events: list[Event] = Field(..., min_length=1)
    constraints: list[GapConstraint] = Field(default_factory=list)


class TightenedEvent(BaseModel):
    id: str
    earliest: str = Field(..., description="Tightened earliest time, RFC 3339 UTC")
    latest: str = Field(..., description="Tightened latest time, RFC 3339 UTC")
    feasible_time: str = Field(..., description="One feasible concrete timestamp, RFC 3339 UTC")


class FeasibleAudit(BaseModel):
    feasible: Literal[True] = True
    events: list[TightenedEvent]


class ConflictStep(BaseModel):
    kind: Literal["earliest", "latest", "at_least", "at_most"]
    constraint_index: Optional[int] = None
    constraint_id: Optional[str] = None
    event: Optional[str] = None
    left_event: Optional[str] = None
    right_event: Optional[str] = None
    bound: Optional[str] = None
    seconds: Optional[str] = None
    inequality: str


class ConflictLoop(BaseModel):
    events: list[str] = Field(
        ..., description="Events on the closed walk, in order; first and last are equal"
    )
    steps: list[ConflictStep]
    total_seconds: str = Field(..., description="Sum of the loop's gap bounds (negative)")
    contradiction: str


class InfeasibleAudit(BaseModel):
    feasible: Literal[False] = False
    conflict: ConflictLoop


AuditResponse = FeasibleAudit | InfeasibleAudit


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Optional[dict] = None


class ErrorEnvelope(BaseModel):
    error: ErrorDetail
