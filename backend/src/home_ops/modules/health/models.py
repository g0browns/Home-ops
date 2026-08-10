"""Health records (SPEC §4.8).

**The most sensitive data in the system, and the schema is where that is either
true or merely claimed.** Three decisions carry it.

**1. The subject is not the author.** Every record carries `owner_id` — who it
is *about* — and `recorded_by_id` — who typed it. A parent logging a toddler's
weight writes a record the *child* owns. Visibility is evaluated against the
subject, so writing a record for somebody does not grant continuing sight of
their history: the parent sees it afterwards only if the child's share list says
so. Settled with the owner on 2026-08-01, and needed from day one because
splitting the two later is a migration across exactly the table you least want
to migrate.

**2. Sharing is per person, not per record.** `HealthShare` is
`(subject_id, viewer_id)`. Nobody wants to share a blood pressure reading one at
a time; §4.8 says "private to the subject by default, sharing explicit and
revocable", and a person is the unit people actually think in. Revoking is
deleting one row, and it takes effect on the next read because visibility is
computed, never copied onto the records.

**3. Every health row is `assignees`-visible, and a CHECK says so.** These
models mix in `OwnedVisibleMixin` so the scoping guard refuses to serve them
unscoped — that protection matters more here than anywhere else in the app. But
`household` must never be reachable: a health record visible to the whole
household by a mistyped field is the failure this module exists to prevent. So
the column is pinned to one value by a constraint, and the share table is the
only control. An empty share list *is* "private to the subject".

**There is no admin branch, here or in `scoping.visible`.** §4.8 requires that
in words; `test_health.py` requires it in assertions.

**Statistics, never interpretations.** Nothing in this schema stores a
judgment. Lab reference ranges are columns on the *analyte row* because they
came off somebody's own lab report — they are that lab's numbers, recorded, not
ours computed. No column here holds "high", "low", "abnormal" or a risk score,
and none ever should.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ColumnElement,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    and_,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from home_ops.db import Base
from home_ops.policy import Principal, Visibility
from home_ops.scoping import OwnedVisibleMixin

#: What a vital sign can be. A closed vocabulary rather than free text, because
#: a trend chart needs to know what it is charting and "BP"/"blood pressure"/
#: "Blood Pressure" as three series is a chart of nothing.
VITAL_KINDS: tuple[str, ...] = (
    "weight",
    "blood_pressure",
    "heart_rate",
    "blood_glucose",
    "temperature",
    "oxygen_saturation",
    "respiratory_rate",
    "custom",
)

#: How a medication is taken. Mealie-style closed list, for the same reason.
MED_FORMS: tuple[str, ...] = (
    "tablet",
    "capsule",
    "liquid",
    "injection",
    "inhaler",
    "patch",
    "drops",
    "cream",
    "other",
)

ACTIVITY_KINDS: tuple[str, ...] = (
    "walk",
    "run",
    "cycle",
    "swim",
    "gym",
    "sport",
    "other",
)


class HealthShare(Base):
    """One member letting another see their health records.

    Explicit and revocable, per §4.8: a row exists or it does not, and deleting
    it takes effect on the next read. `granted_by_id` records who set it up,
    because a share somebody does not remember agreeing to is worth being able
    to trace.
    """

    __tablename__ = "health_shares"

    #: Whose records are being shared.
    subject_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    #: Who may see them.
    viewer_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    granted_by_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    granted_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("subject_id <> viewer_id", name="no_self_share"),
        Index("ix_health_shares_viewer_id", "viewer_id"),
    )


class HealthRecordMixin(OwnedVisibleMixin):
    """What every health table carries, and the visibility rule they share.

    `owner_id`, `visibility` and `created_at` come from `OwnedVisibleMixin`;
    `owner_id` is the **subject**. This adds the author and pins the visibility.
    """

    @classmethod
    def assignee_clause(cls, principal: Principal) -> ColumnElement[bool]:
        """ "This record's subject has shared with me."

        Keyed on the record's `owner_id`, which is the subject — so one share
        row covers every record that person has and every record they ever add,
        without anything being copied onto the rows themselves. Revoking is one
        DELETE and is effective immediately.
        """
        return (
            select(HealthShare.subject_id)
            .where(
                and_(
                    HealthShare.subject_id == cls.owner_id,
                    HealthShare.viewer_id == principal.id,
                )
            )
            .exists()
        )


def _health_table_args(*extra: object) -> tuple[object, ...]:
    """The constraint every health table carries, plus its own.

    Pinning `visibility` to `assignees` is what makes "private by default" a
    property of the schema rather than of whichever code path happened to write
    the row. With an empty share list it means the subject alone; `household` is
    unreachable, which is the point.
    """
    return (
        CheckConstraint("visibility = 'assignees'", name="never_household_visible"),
        *extra,
    )


class VitalReading(HealthRecordMixin, Base):
    """One measurement, at one moment (SPEC §4.8 — vitals with trend charts)."""

    __tablename__ = "vital_readings"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    recorded_by_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: For `custom` — "peak flow", "ketones". Ignored for the known kinds, whose
    #: names come from the frontend so they can be translated in one place.
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: The number. Blood pressure needs two, so it has a second column rather
    #: than a string: "120/80" cannot be charted, averaged or exported as data.
    value: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    secondary_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    #: Stored as the member entered it — kg or lb, mmol/L or mg/dL — because
    #: converting on the way in loses what they actually read off the scale.
    unit: Mapped[str] = mapped_column(String(16), nullable=False)

    measured_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = _health_table_args(
        CheckConstraint("length(trim(unit)) > 0", name="unit_not_blank"),
        Index("ix_vital_readings_owner_kind_time", "owner_id", "kind", "measured_at"),
    )


class Medication(HealthRecordMixin, Base):
    """Something somebody takes (SPEC §4.8 — medications and refill alerts)."""

    __tablename__ = "medications"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    recorded_by_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Free text — "500 mg", "2 puffs", "10 units". Not parsed into a number and
    #: a unit, because a dose is written a hundred ways and getting it wrong is
    #: worse than not modelling it.
    dose: Mapped[str | None] = mapped_column(String(100), nullable=True)
    form: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: When to take it, in words: "morning and night", "with food". §4.3's rrule
    #: machinery is deliberately not reached for — a medication schedule is not a
    #: calendar series, and reminders arrive with §4.11.
    schedule: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: True while it is being taken. Stopped medications stay on the record,
    #: because "what were you on in March" is a real question.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    started_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    stopped_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    #: Stock on hand and the level at which to say something. Both optional:
    #: plenty of medications are not counted, and a warning nobody asked for is
    #: noise.
    stock_count: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    refill_at: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)

    note: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    doses: Mapped[list[MedicationDose]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, lazy="selectin"
    )

    __table_args__ = _health_table_args(
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        CheckConstraint("stock_count IS NULL OR stock_count >= 0", name="stock_not_negative"),
        Index("ix_medications_owner_id", "owner_id"),
    )


class MedicationDose(Base):
    """One dose actually taken — §4.8's as-needed log.

    No visibility of its own: it inherits the medication's, and is only ever
    read through one. Taking a dose decrements the medication's stock, which is
    what makes the refill warning mean anything.
    """

    __tablename__ = "medication_doses"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    medication_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("medications.id", ondelete="CASCADE"), nullable=False
    )
    taken_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    recorded_by_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (Index("ix_medication_doses_medication_id", "medication_id", "taken_at"),)


class LabReport(HealthRecordMixin, Base):
    """A report header — who ran it, when, what it was (SPEC §4.8)."""

    __tablename__ = "lab_reports"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    recorded_by_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Who ran it. Free text: a household will write "Quest", "the GP", "Dr
    #: Weaver's office" and all three are the right answer to them.
    lab_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    collected_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    analytes: Mapped[list[LabAnalyte]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="LabAnalyte.position",
    )

    __table_args__ = _health_table_args(
        CheckConstraint("length(trim(title)) > 0", name="title_not_blank"),
        Index("ix_lab_reports_owner_collected", "owner_id", "collected_on"),
    )


class LabAnalyte(Base):
    """One line off a lab report: what was measured, and what the lab called normal.

    **The reference range is the lab's, transcribed — never ours, computed.**
    That is the whole of §4.8's "statistics, never interpretations" in one
    column pair: a household copies the numbers printed on their own report, and
    the app shows them back. Nothing here decides whether a value is good, and
    no column stores a verdict.
    """

    __tablename__ = "lab_analytes"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    report_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("lab_reports.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    #: Some results are words — "negative", "trace". Kept beside the number
    #: rather than instead of it, so a numeric result is still chartable.
    text_value: Mapped[str | None] = mapped_column(String(120), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The range as printed on the report. Optional, because plenty of lines
    #: have none, and blank is honest where a made-up range would not be.
    reference_low: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    reference_high: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    reference_text: Mapped[str | None] = mapped_column(String(120), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        Index("ix_lab_analytes_report_id", "report_id"),
    )


class ActivityEntry(HealthRecordMixin, Base):
    """One walk, run, ride or session (SPEC §4.8 — activity)."""

    __tablename__ = "activity_entries"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    recorded_by_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    happened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Miles, and the column says so rather than leaving "distance" to be
    #: guessed at. US measures throughout, settled 2026-08-01.
    distance_miles: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    calories: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = _health_table_args(
        CheckConstraint(
            "duration_minutes IS NULL OR duration_minutes > 0", name="duration_positive"
        ),
        Index("ix_activity_entries_owner_time", "owner_id", "happened_at"),
    )


#: Every visibility-bearing health model, for the tests that sweep all of them.
HEALTH_MODELS: tuple[type[HealthRecordMixin], ...] = (
    VitalReading,
    Medication,
    LabReport,
    ActivityEntry,
)

#: The one value any health row's visibility may hold. Exported so the service
#: never writes the literal and the tests can assert against the same name.
HEALTH_VISIBILITY = Visibility.ASSIGNEES
