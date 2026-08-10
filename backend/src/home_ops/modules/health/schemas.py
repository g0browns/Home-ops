"""Request and response shapes for health records (SPEC §4.8).

**No response model here carries a verdict.** There is no `status`, no `flag`,
no `is_normal`, no risk score, and `test_health.py` asserts their absence by
name. §4.8: "present statistics, never interpretations." A lab reference range
travels as the two numbers somebody transcribed off their own report; nothing
compares a value to them.

`visibility` is absent from every request shape on purpose. A health record has
exactly one visibility, the database has a CHECK saying so, and the share list
is the only control — so there is no field for a client to get wrong.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, Field

from home_ops.modules.health.models import ACTIVITY_KINDS, MED_FORMS, VITAL_KINDS

Stripped = BeforeValidator(lambda value: value.strip() if isinstance(value, str) else value)

# Constraints sit on the `str` side of every union, never on `str | None` —
# pydantic measures the length of None otherwise and raises a TypeError, which
# is a 500 rather than a 422, the moment a client sends an explicit null.
Label = Annotated[str, Stripped, Field(max_length=64)]
Note = Annotated[str, Stripped, Field(max_length=500)]
LongNote = Annotated[str, Stripped, Field(max_length=10_000)]
Unit = Annotated[str, Stripped, Field(min_length=1, max_length=16)]
Name = Annotated[str, Stripped, Field(min_length=1, max_length=200)]

VitalKind = Literal[VITAL_KINDS]  # type: ignore[valid-type]
ActivityKind = Literal[ACTIVITY_KINDS]  # type: ignore[valid-type]
MedForm = Literal[MED_FORMS]  # type: ignore[valid-type]

#: A measurement, bounded to keep a typo out of a chart's axis rather than to
#: express any opinion about what a body may weigh.
Measurement = Annotated[Decimal, Field(ge=0, le=Decimal("100000"))]


# --- sharing ------------------------------------------------------------------


class ShareOut(BaseModel):
    viewer_id: UUID
    granted_at: dt.datetime


class SharesIn(BaseModel):
    """Who may see my records. Replacing the list is how revoking works."""

    viewer_ids: Annotated[list[UUID], Field(max_length=50)] = Field(default_factory=list)


# --- vitals -------------------------------------------------------------------


class VitalIn(BaseModel):
    subject_id: UUID
    kind: VitalKind
    label: Label | None = None
    value: Measurement
    #: The diastolic half of a blood pressure. Two columns, not "120/80" in a
    #: string, because a string cannot be charted or averaged.
    secondary_value: Measurement | None = None
    unit: Unit
    measured_at: dt.datetime
    note: Note | None = None


class VitalOut(BaseModel):
    id: UUID
    subject_id: UUID
    recorded_by_id: UUID | None
    kind: str
    label: str | None
    value: Decimal
    secondary_value: Decimal | None
    unit: str
    measured_at: dt.datetime
    note: str | None


class VitalSummary(BaseModel):
    """Descriptive statistics, and nothing else.

    Every field is arithmetic over what the member recorded. `change` is the
    plain difference between the oldest and newest reading — a number, not a
    direction with a meaning attached.
    """

    kind: str
    unit: str | None
    count: int
    first_at: dt.datetime | None
    last_at: dt.datetime | None
    latest: Decimal | None
    minimum: Decimal | None
    maximum: Decimal | None
    mean: Decimal | None
    change: Decimal | None


# --- medications --------------------------------------------------------------


class MedicationIn(BaseModel):
    subject_id: UUID
    name: Name
    dose: Annotated[str, Stripped, Field(max_length=100)] | None = None
    form: MedForm | None = None
    schedule: Annotated[str, Stripped, Field(max_length=200)] | None = None
    started_on: dt.date | None = None
    stopped_on: dt.date | None = None
    stock_count: Annotated[Decimal, Field(ge=0, le=Decimal("100000"))] | None = None
    refill_at: Annotated[Decimal, Field(ge=0, le=Decimal("100000"))] | None = None
    note: Annotated[str, Stripped, Field(max_length=1000)] | None = None


class MedicationPatch(BaseModel):
    name: Name | None = None
    dose: Annotated[str, Stripped, Field(max_length=100)] | None = None
    form: MedForm | None = None
    schedule: Annotated[str, Stripped, Field(max_length=200)] | None = None
    is_active: bool | None = None
    started_on: dt.date | None = None
    stopped_on: dt.date | None = None
    stock_count: Annotated[Decimal, Field(ge=0, le=Decimal("100000"))] | None = None
    refill_at: Annotated[Decimal, Field(ge=0, le=Decimal("100000"))] | None = None
    note: Annotated[str, Stripped, Field(max_length=1000)] | None = None


class DoseOut(BaseModel):
    id: UUID
    taken_at: dt.datetime
    amount: Decimal | None
    recorded_by_id: UUID | None
    note: str | None


class DoseIn(BaseModel):
    taken_at: dt.datetime | None = None
    amount: Annotated[Decimal, Field(gt=0, le=Decimal("10000"))] | None = None
    note: Note | None = None


class MedicationOut(BaseModel):
    id: UUID
    subject_id: UUID
    recorded_by_id: UUID | None
    name: str
    dose: str | None
    form: str | None
    schedule: str | None
    is_active: bool
    started_on: dt.date | None
    stopped_on: dt.date | None
    stock_count: Decimal | None
    refill_at: Decimal | None
    #: Stock at or below the level *the member set*. A comparison between two
    #: numbers they supplied about a box of pills — not a judgment about them.
    needs_refill: bool
    note: str | None
    doses: list[DoseOut] = Field(default_factory=list)


# --- lab results --------------------------------------------------------------


class AnalyteIn(BaseModel):
    name: Annotated[str, Stripped, Field(min_length=1, max_length=120)]
    value: Annotated[Decimal, Field(ge=Decimal("-100000"), le=Decimal("1000000"))] | None = None
    text_value: Annotated[str, Stripped, Field(max_length=120)] | None = None
    unit: Annotated[str, Stripped, Field(max_length=32)] | None = None
    #: Transcribed from the report, never computed. Optional, because plenty of
    #: lines have no printed range and blank is honest where a guess would not
    #: be.
    reference_low: (
        Annotated[Decimal, Field(ge=Decimal("-100000"), le=Decimal("1000000"))] | None
    ) = None
    reference_high: (
        Annotated[Decimal, Field(ge=Decimal("-100000"), le=Decimal("1000000"))] | None
    ) = None
    reference_text: Annotated[str, Stripped, Field(max_length=120)] | None = None


class AnalyteOut(BaseModel):
    id: UUID
    name: str
    value: Decimal | None
    text_value: str | None
    unit: str | None
    reference_low: Decimal | None
    reference_high: Decimal | None
    reference_text: str | None
    position: int


class LabReportIn(BaseModel):
    subject_id: UUID
    title: Name
    lab_name: Annotated[str, Stripped, Field(max_length=200)] | None = None
    collected_on: dt.date
    note: LongNote | None = None
    analytes: Annotated[list[AnalyteIn], Field(max_length=200)] = Field(default_factory=list)


class LabReportOut(BaseModel):
    id: UUID
    subject_id: UUID
    recorded_by_id: UUID | None
    title: str
    lab_name: str | None
    collected_on: dt.date
    note: str | None
    analytes: list[AnalyteOut] = Field(default_factory=list)


# --- activity -----------------------------------------------------------------


class ActivityIn(BaseModel):
    subject_id: UUID
    kind: ActivityKind
    label: Label | None = None
    happened_at: dt.datetime
    duration_minutes: Annotated[int, Field(gt=0, le=100_000)] | None = None
    #: Miles. US measures throughout, settled 2026-08-01.
    distance_miles: Annotated[Decimal, Field(ge=0, le=Decimal("100000"))] | None = None
    calories: Annotated[int, Field(ge=0, le=1_000_000)] | None = None
    note: Note | None = None


class ActivityOut(BaseModel):
    id: UUID
    subject_id: UUID
    recorded_by_id: UUID | None
    kind: str
    label: str | None
    happened_at: dt.datetime
    duration_minutes: int | None
    distance_miles: Decimal | None
    calories: int | None
    note: str | None


class VocabularyOut(BaseModel):
    """The closed lists, so the frontend cannot drift from the database."""

    vital_kinds: list[str]
    medication_forms: list[str]
    activity_kinds: list[str]
