"""Health record operations (SPEC §4.8).

Every read goes through `visible()`, which for these models resolves to "I am
the subject, or the subject has shared with me". There is no admin branch and
there must never be one: §4.8 says an admin gets no implicit access to another
member's records, `scoping.visible` contains no role check at all, and
`test_health.py` asserts the outcome rather than trusting either statement.

**Writing for somebody else is not reading for somebody else.** A parent may
record a child's weight — `can_record_for` decides that — and the record belongs
to the child. Whether the parent can *see* it afterwards is a separate question
answered by the child's share list, which is why the two functions are separate
and neither calls the other.

**Nothing here computes a judgment.** `summarise` returns count, first, last,
latest, minimum, maximum and mean, and stops. §4.8: "present statistics, never
interpretations" — no thresholds, no flags, no risk scores, no "normal". A lab
reference range is transcribed from somebody's own report and shown back
unchanged; nothing compares a value to it on the server.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Select, delete, select
from sqlalchemy.orm import Session as DbSession

from home_ops.modules.health.models import (
    HEALTH_VISIBILITY,
    ActivityEntry,
    HealthShare,
    LabReport,
    Medication,
    MedicationDose,
    VitalReading,
)
from home_ops.modules.identity.models import User
from home_ops.policy import Principal, Role
from home_ops.scoping import SCOPED_OPTION, visible

#: The widest window a chart or an export may ask for. Long enough for "the last
#: five years of my weight", short enough that nothing unbounded is answerable.
MAX_SPAN = dt.timedelta(days=366 * 5)


class NotFound(LookupError):
    pass


class SpanTooWide(ValueError):
    pass


class NotYours(PermissionError):
    """Writing a record for somebody who has not allowed it."""


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# --- who may do what ----------------------------------------------------------


def can_record_for(db: DbSession, principal: Principal, subject_id: UUID) -> bool:
    """May this caller write a record *about* `subject_id`?

    Yourself, always. Somebody else only if you are an adult or admin **and**
    they are a `limited` member — the household case this exists for is a parent
    logging a young child's weight, not one adult writing in another adult's
    record. An adult who wants somebody else keeping their notes can share, and
    sharing is a separate, revocable thing.
    """
    if principal.id == subject_id:
        return True
    if principal.role not in (Role.ADMIN, Role.ADULT):
        return False

    subject = db.get(User, subject_id)
    return subject is not None and subject.is_active and subject.role == Role.LIMITED


def subjects_visible_to(db: DbSession, principal: Principal) -> list[UUID]:
    """Whose records this caller may read: themselves, plus anyone sharing."""
    shared = db.scalars(
        select(HealthShare.subject_id)
        .where(HealthShare.viewer_id == principal.id)
        # The share table carries no visibility of its own; it *is* the
        # visibility. Reading your own incoming shares discloses nothing you
        # were not already granted.
        .execution_options(**{SCOPED_OPTION: True})
    )
    return [principal.id, *shared]


def list_shares(db: DbSession, subject_id: UUID) -> list[HealthShare]:
    """Who `subject_id` has shared with. Only ever called for yourself."""
    return list(
        db.scalars(
            select(HealthShare)
            .where(HealthShare.subject_id == subject_id)
            .execution_options(**{SCOPED_OPTION: True})
        )
    )


def set_shares(
    db: DbSession, principal: Principal, subject_id: UUID, viewer_ids: list[UUID]
) -> None:
    """Replace who may see `subject_id`'s records.

    Revocation is a DELETE and takes effect on the next read, because nothing is
    copied onto the records themselves — §4.8's "revocable" in the only form
    that actually holds.
    """
    db.execute(delete(HealthShare).where(HealthShare.subject_id == subject_id))
    for viewer_id in dict.fromkeys(viewer_ids):
        if viewer_id == subject_id:
            # The CHECK would refuse it anyway; skipping keeps a pointless row
            # from turning a tidy-up into a 500.
            continue
        db.add(HealthShare(subject_id=subject_id, viewer_id=viewer_id, granted_by_id=principal.id))
    db.flush()


# --- reading ------------------------------------------------------------------


def _scoped(model: Any, principal: Principal) -> Select[Any]:
    return visible(select(model), model, principal)


def list_vitals(
    db: DbSession,
    principal: Principal,
    *,
    subject_id: UUID | None = None,
    kind: str | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    limit: int = 2000,
) -> list[VitalReading]:
    stmt = _scoped(VitalReading, principal)
    if subject_id is not None:
        stmt = stmt.where(VitalReading.owner_id == subject_id)
    if kind:
        stmt = stmt.where(VitalReading.kind == kind)
    if since is not None:
        stmt = stmt.where(VitalReading.measured_at >= since)
    if until is not None:
        stmt = stmt.where(VitalReading.measured_at <= until)
    stmt = stmt.order_by(VitalReading.measured_at.desc()).limit(limit)
    return list(db.scalars(stmt).unique())


def list_medications(
    db: DbSession,
    principal: Principal,
    *,
    subject_id: UUID | None = None,
    include_stopped: bool = False,
) -> list[Medication]:
    stmt = _scoped(Medication, principal)
    if subject_id is not None:
        stmt = stmt.where(Medication.owner_id == subject_id)
    if not include_stopped:
        stmt = stmt.where(Medication.is_active.is_(True))
    return list(db.scalars(stmt.order_by(Medication.name)).unique())


def list_lab_reports(
    db: DbSession, principal: Principal, *, subject_id: UUID | None = None
) -> list[LabReport]:
    stmt = _scoped(LabReport, principal)
    if subject_id is not None:
        stmt = stmt.where(LabReport.owner_id == subject_id)
    return list(db.scalars(stmt.order_by(LabReport.collected_on.desc())).unique())


def list_activity(
    db: DbSession,
    principal: Principal,
    *,
    subject_id: UUID | None = None,
    since: dt.datetime | None = None,
    limit: int = 2000,
) -> list[ActivityEntry]:
    stmt = _scoped(ActivityEntry, principal)
    if subject_id is not None:
        stmt = stmt.where(ActivityEntry.owner_id == subject_id)
    if since is not None:
        stmt = stmt.where(ActivityEntry.happened_at >= since)
    return list(db.scalars(stmt.order_by(ActivityEntry.happened_at.desc()).limit(limit)).unique())


def get_record(db: DbSession, principal: Principal, model: Any, record_id: UUID) -> Any:
    """One record of any health kind, or `NotFound`.

    Missing rather than forbidden: "you may not see this" confirms that a record
    exists, which for health data is most of what somebody was asking.
    """
    found = (
        db.scalars(_scoped(model, principal).where(model.id == record_id)).unique().one_or_none()
    )
    if found is None:
        raise NotFound(str(record_id))
    return found


# --- statistics, and nothing beyond them --------------------------------------


class Summary:
    """Count, span, and the plain descriptive numbers. Deliberately no verdict.

    §4.8: "Show trends, ranges, and changes over time. Do not generate health
    assessments, risk scores, or anything resembling advice." Everything here is
    arithmetic over what the member recorded; there is no threshold to compare
    against and none should ever be added.
    """

    def __init__(self, readings: list[VitalReading]) -> None:
        values = [reading.value for reading in readings]
        self.count = len(readings)
        self.first_at = min((r.measured_at for r in readings), default=None)
        self.last_at = max((r.measured_at for r in readings), default=None)
        self.latest = next(
            (r.value for r in sorted(readings, key=lambda r: r.measured_at, reverse=True)), None
        )
        self.minimum = min(values, default=None)
        self.maximum = max(values, default=None)
        # `sum([])` is int 0, so the start value keeps this Decimal end to end
        # rather than becoming a float the moment the list is empty.
        total = sum(values, Decimal(0))
        self.mean = (total / len(values)).quantize(Decimal("0.001")) if values else None
        #: The plain difference between the oldest and newest reading. A number,
        #: not a direction with a meaning attached: "-2.4" is a fact, "improving"
        #: would be an interpretation.
        ordered = sorted(readings, key=lambda r: r.measured_at)
        self.change = ordered[-1].value - ordered[0].value if len(ordered) > 1 else None


def summarise(readings: list[VitalReading]) -> Summary:
    return Summary(readings)


# --- writing ------------------------------------------------------------------


def create(db: DbSession, model: Any, *, subject_id: UUID, author_id: UUID, **fields: Any) -> Any:
    """Any health record: owned by the subject, stamped with the author.

    `visibility` is set here and nowhere else, to the one value the CHECK
    allows. A caller cannot pass it in, which is the point.
    """
    record = model(
        owner_id=subject_id,
        recorded_by_id=author_id,
        visibility=HEALTH_VISIBILITY.value,
        **fields,
    )
    db.add(record)
    db.flush()
    return record


def reload_written(db: DbSession, model: Any, record_id: UUID) -> Any:
    """Re-read a row the caller has just written, unscoped and deliberately.

    Two things make this safe, and both matter:

    * The id comes from the row this request created a moment ago — never from
      the caller — so there is nothing to point at somebody else's record.
    * The caller supplied every value in it. Handing it back discloses nothing
      they did not just type.

    It exists because the alternative is worse in both directions. Returning the
    in-memory object answers `120` where every later GET says `120.000`, since
    the value has not been through a `Numeric(10,3)` column yet. Re-reading
    through the *scoped* query would 404 on exactly the case the subject/author
    split exists for — a parent recording a child's weight cannot read the
    child's records, so the create would fail after having succeeded.
    """
    return (
        db.scalars(
            select(model)
            .where(model.id == record_id)
            # Justified above. The guard is doing its job by making this the
            # only place in the module that has to argue for itself.
            #
            # `populate_existing` is what makes this a re-read at all: the
            # session is built with `expire_on_commit=False`, so without it the
            # identity map hands back the instance already in memory and the
            # round trip achieves nothing.
            .execution_options(populate_existing=True, **{SCOPED_OPTION: True})
        )
        .unique()
        .one()
    )


def delete_record(db: DbSession, record: Any) -> None:
    """§4.8 requires deletion to work. It is a real DELETE, not a flag."""
    db.delete(record)
    db.flush()


def log_dose(
    db: DbSession,
    medication: Medication,
    *,
    author_id: UUID,
    taken_at: dt.datetime,
    amount: Decimal | None = None,
    note: str | None = None,
) -> MedicationDose:
    """Record a dose, and take it off the stock.

    Decrementing here is what makes the refill warning mean anything: a count
    that only ever goes up when a box is added is a count nobody trusts. It
    floors at zero rather than going negative, because a negative stock is a
    data-entry story, not a fact about a cupboard.
    """
    dose = MedicationDose(
        medication_id=medication.id,
        taken_at=taken_at,
        amount=amount,
        recorded_by_id=author_id,
        note=note,
    )
    db.add(dose)

    if medication.stock_count is not None:
        taken = amount if amount is not None else Decimal(1)
        medication.stock_count = max(Decimal(0), medication.stock_count - taken)

    db.flush()
    return dose


def needs_refill(medication: Medication) -> bool:
    """Stock at or below the level the member asked to be warned at.

    A comparison between two numbers the member supplied. That is a threshold
    they set about a box of pills, not a judgment we made about their health,
    which is why it is allowed to exist while nothing similar exists for vitals.
    """
    if medication.stock_count is None or medication.refill_at is None:
        return False
    return medication.stock_count <= medication.refill_at


# --- export -------------------------------------------------------------------


def export_csv(db: DbSession, principal: Principal, subject_id: UUID) -> str:
    """One subject's records as CSV (§4.8 requires export to work).

    Scoped like every other read, so an export can only ever contain what the
    caller could already see. One file with a `record_type` column rather than
    four files: a household wants "my health data", and a zip of four CSVs is a
    worse answer to that than one sheet they can filter.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "record_type",
            "when",
            "name",
            "value",
            "secondary_value",
            "unit",
            "reference_low",
            "reference_high",
            "note",
        ]
    )

    for reading in list_vitals(db, principal, subject_id=subject_id, limit=100_000):
        writer.writerow(
            [
                "vital",
                reading.measured_at.isoformat(),
                reading.label or reading.kind,
                reading.value,
                reading.secondary_value if reading.secondary_value is not None else "",
                reading.unit,
                "",
                "",
                reading.note or "",
            ]
        )

    for medication in list_medications(db, principal, subject_id=subject_id, include_stopped=True):
        writer.writerow(
            [
                "medication",
                medication.started_on.isoformat() if medication.started_on else "",
                medication.name,
                medication.dose or "",
                "",
                medication.form or "",
                "",
                "",
                medication.note or "",
            ]
        )
        for dose in medication.doses:
            writer.writerow(
                [
                    "medication_dose",
                    dose.taken_at.isoformat(),
                    medication.name,
                    dose.amount if dose.amount is not None else "",
                    "",
                    "",
                    "",
                    "",
                    dose.note or "",
                ]
            )

    for report in list_lab_reports(db, principal, subject_id=subject_id):
        for analyte in report.analytes:
            writer.writerow(
                [
                    "lab_result",
                    report.collected_on.isoformat(),
                    analyte.name,
                    analyte.value if analyte.value is not None else (analyte.text_value or ""),
                    "",
                    analyte.unit or "",
                    analyte.reference_low if analyte.reference_low is not None else "",
                    analyte.reference_high if analyte.reference_high is not None else "",
                    # The report's title, not a verdict on the number.
                    report.title,
                ]
            )

    for entry in list_activity(db, principal, subject_id=subject_id, limit=100_000):
        writer.writerow(
            [
                "activity",
                entry.happened_at.isoformat(),
                entry.label or entry.kind,
                entry.duration_minutes if entry.duration_minutes is not None else "",
                entry.distance_miles if entry.distance_miles is not None else "",
                "minutes / miles",
                "",
                "",
                entry.note or "",
            ]
        )

    return buffer.getvalue()
