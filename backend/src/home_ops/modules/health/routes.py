"""Health record endpoints (SPEC §4.8).

Mounted at `/api/health-records`, **not** `/api/health` — that URL is the
application's liveness probe, is public, and is verified over the Cloudflare
hostname by §8.6. Moving it to make room for this would be trading a check that
must never break for a tidier noun.

Two rules run through every handler here:

* **A record you may not see is a 404**, never a 403. "You are not allowed to
  see this" confirms the record exists, and for health data that is most of what
  somebody was asking.
* **Reading and writing are separate questions.** `can_record_for` decides
  whether you may write a record about somebody; their share list decides
  whether you may read it afterwards. A parent logging a child's weight does not
  thereby gain sight of the child's history.

Every state-changing call is audited with *what kind* of record moved and whose
it was — never a value. An audit row holding a blood pressure reading has copied
the most sensitive data in the app into a second table with different
permissions.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from home_ops import audit
from home_ops.audit import AuditAction
from home_ops.dependencies import AuthContext, AuthDep, ClientIpDep, DbDep, require
from home_ops.modules.health import schemas, service
from home_ops.modules.health.models import (
    ACTIVITY_KINDS,
    MED_FORMS,
    VITAL_KINDS,
    ActivityEntry,
    LabAnalyte,
    LabReport,
    Medication,
    VitalReading,
)
from home_ops.policy import Action, Module

router = APIRouter(prefix="/health-records", tags=["health"])


def _require_subject(db: DbDep, auth: AuthContext, subject_id: UUID) -> None:
    if not service.can_record_for(db, auth.principal, subject_id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="You can only record health data for yourself.",
        )


def _get(db: DbDep, auth: AuthContext, model: Any, record_id: UUID) -> Any:
    try:
        return service.get_record(db, auth.principal, model, record_id)
    except service.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such record.") from exc


def _note(
    db: DbDep,
    auth: AuthContext,
    client_ip: str | None,
    action: AuditAction,
    *,
    kind: str,
    subject_id: UUID,
    record_id: UUID | None = None,
) -> None:
    """Audit the shape of the change, never its contents."""
    audit.record(
        db,
        action,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type=f"health.{kind}",
        resource_id=str(record_id) if record_id else None,
        client_ip=client_ip,
        detail={"subject_id": str(subject_id), "for_self": subject_id == auth.user.id},
    )


# --- vocabulary and sharing ---------------------------------------------------


@router.get(
    "/vocabulary",
    response_model=schemas.VocabularyOut,
    dependencies=[Depends(require(Action.READ, Module.HEALTH))],
    summary="The closed lists the frontend draws from",
)
def vocabulary() -> schemas.VocabularyOut:
    return schemas.VocabularyOut(
        vital_kinds=list(VITAL_KINDS),
        medication_forms=list(MED_FORMS),
        activity_kinds=list(ACTIVITY_KINDS),
    )


@router.get(
    "/shares",
    response_model=list[schemas.ShareOut],
    dependencies=[Depends(require(Action.READ, Module.HEALTH))],
    summary="Who you have shared your records with",
)
def get_shares(db: DbDep, auth: AuthDep) -> list[schemas.ShareOut]:
    # Always your own list. There is no endpoint for reading somebody else's,
    # because who a person has shared with is itself health information.
    return [
        schemas.ShareOut(viewer_id=share.viewer_id, granted_at=share.granted_at)
        for share in service.list_shares(db, auth.user.id)
    ]


@router.put(
    "/shares",
    response_model=list[schemas.ShareOut],
    dependencies=[Depends(require(Action.WRITE, Module.HEALTH))],
    summary="Replace who may see your records",
)
def put_shares(
    payload: schemas.SharesIn, db: DbDep, auth: AuthDep, client_ip: ClientIpDep
) -> list[schemas.ShareOut]:
    service.set_shares(db, auth.principal, auth.user.id, payload.viewer_ids)
    # Worth an audit row: this is the control that decides who reads the most
    # sensitive data in the app, and §4.8 wants sharing to be traceable.
    audit.record(
        db,
        AuditAction.HEALTH_SHARING_CHANGED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="health.shares",
        client_ip=client_ip,
        detail={"viewers": len(payload.viewer_ids)},
    )
    db.commit()
    return [
        schemas.ShareOut(viewer_id=share.viewer_id, granted_at=share.granted_at)
        for share in service.list_shares(db, auth.user.id)
    ]


@router.get(
    "/subjects",
    response_model=list[UUID],
    dependencies=[Depends(require(Action.READ, Module.HEALTH))],
    summary="Whose records you can see",
)
def subjects(db: DbDep, auth: AuthDep) -> list[UUID]:
    return service.subjects_visible_to(db, auth.principal)


# --- vitals -------------------------------------------------------------------


def _vital_out(reading: VitalReading) -> schemas.VitalOut:
    return schemas.VitalOut(
        id=reading.id,
        subject_id=reading.owner_id,
        recorded_by_id=reading.recorded_by_id,
        kind=reading.kind,
        label=reading.label,
        value=reading.value,
        secondary_value=reading.secondary_value,
        unit=reading.unit,
        measured_at=reading.measured_at,
        note=reading.note,
    )


@router.get(
    "/vitals",
    response_model=list[schemas.VitalOut],
    dependencies=[Depends(require(Action.READ, Module.HEALTH))],
    summary="Readings you can see",
)
def list_vitals(
    db: DbDep,
    auth: AuthDep,
    subject_id: UUID | None = None,
    kind: Annotated[str | None, Query(max_length=32)] = None,
    since: dt.datetime | None = None,
) -> list[schemas.VitalOut]:
    readings = service.list_vitals(
        db, auth.principal, subject_id=subject_id, kind=kind, since=since
    )
    return [_vital_out(reading) for reading in readings]


@router.get(
    "/vitals/summary",
    response_model=schemas.VitalSummary,
    dependencies=[Depends(require(Action.READ, Module.HEALTH))],
    summary="Descriptive statistics for one kind of reading",
)
def vital_summary(
    db: DbDep,
    auth: AuthDep,
    subject_id: UUID,
    kind: Annotated[str, Query(max_length=32)],
    since: dt.datetime | None = None,
) -> schemas.VitalSummary:
    """Count, range, mean and change. Nothing that reads as advice.

    §4.8 is explicit, so this endpoint is deliberately dull: there is no
    threshold to compare against, no flag, and no wording anywhere that could be
    mistaken for an assessment.
    """
    readings = service.list_vitals(
        db, auth.principal, subject_id=subject_id, kind=kind, since=since
    )
    stats = service.summarise(readings)
    return schemas.VitalSummary(
        kind=kind,
        unit=readings[0].unit if readings else None,
        count=stats.count,
        first_at=stats.first_at,
        last_at=stats.last_at,
        latest=stats.latest,
        minimum=stats.minimum,
        maximum=stats.maximum,
        mean=stats.mean,
        change=stats.change,
    )


@router.post(
    "/vitals",
    response_model=schemas.VitalOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Action.WRITE, Module.HEALTH))],
    summary="Record a reading",
)
def add_vital(
    payload: schemas.VitalIn, db: DbDep, auth: AuthDep, client_ip: ClientIpDep
) -> schemas.VitalOut:
    _require_subject(db, auth, payload.subject_id)
    reading = service.create(
        db,
        VitalReading,
        subject_id=payload.subject_id,
        author_id=auth.user.id,
        kind=payload.kind,
        label=payload.label,
        value=payload.value,
        secondary_value=payload.secondary_value,
        unit=payload.unit,
        measured_at=payload.measured_at,
        note=payload.note,
    )
    _note(
        db,
        auth,
        client_ip,
        AuditAction.HEALTH_RECORD_CREATED,
        kind="vital",
        subject_id=payload.subject_id,
        record_id=reading.id,
    )
    db.commit()
    # Re-read rather than returning the in-memory object: the column is
    # Numeric(10,3) and an unrefreshed Decimal("120") answers "120" where every
    # later GET says "120.000". A client that stores what it posted and then
    # compares it against a reload should not see a difference.
    return _vital_out(service.reload_written(db, VitalReading, reading.id))


@router.delete(
    "/vitals/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.HEALTH))],
    summary="Delete a reading",
)
def delete_vital(record_id: UUID, db: DbDep, auth: AuthDep, client_ip: ClientIpDep) -> Response:
    reading = _get(db, auth, VitalReading, record_id)
    _require_subject(db, auth, reading.owner_id)
    _note(
        db,
        auth,
        client_ip,
        AuditAction.HEALTH_RECORD_DELETED,
        kind="vital",
        subject_id=reading.owner_id,
        record_id=record_id,
    )
    service.delete_record(db, reading)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- medications --------------------------------------------------------------


def _medication_out(medication: Medication) -> schemas.MedicationOut:
    return schemas.MedicationOut(
        id=medication.id,
        subject_id=medication.owner_id,
        recorded_by_id=medication.recorded_by_id,
        name=medication.name,
        dose=medication.dose,
        form=medication.form,
        schedule=medication.schedule,
        is_active=medication.is_active,
        started_on=medication.started_on,
        stopped_on=medication.stopped_on,
        stock_count=medication.stock_count,
        refill_at=medication.refill_at,
        needs_refill=service.needs_refill(medication),
        note=medication.note,
        doses=[
            schemas.DoseOut(
                id=dose.id,
                taken_at=dose.taken_at,
                amount=dose.amount,
                recorded_by_id=dose.recorded_by_id,
                note=dose.note,
            )
            for dose in sorted(medication.doses, key=lambda d: d.taken_at, reverse=True)[:50]
        ],
    )


@router.get(
    "/medications",
    response_model=list[schemas.MedicationOut],
    dependencies=[Depends(require(Action.READ, Module.HEALTH))],
    summary="Medications you can see",
)
def list_medications(
    db: DbDep, auth: AuthDep, subject_id: UUID | None = None, include_stopped: bool = False
) -> list[schemas.MedicationOut]:
    return [
        _medication_out(medication)
        for medication in service.list_medications(
            db, auth.principal, subject_id=subject_id, include_stopped=include_stopped
        )
    ]


@router.post(
    "/medications",
    response_model=schemas.MedicationOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Action.WRITE, Module.HEALTH))],
    summary="Add a medication",
)
def add_medication(
    payload: schemas.MedicationIn, db: DbDep, auth: AuthDep, client_ip: ClientIpDep
) -> schemas.MedicationOut:
    _require_subject(db, auth, payload.subject_id)
    fields = payload.model_dump(exclude={"subject_id"})
    medication = service.create(
        db, Medication, subject_id=payload.subject_id, author_id=auth.user.id, **fields
    )
    _note(
        db,
        auth,
        client_ip,
        AuditAction.HEALTH_RECORD_CREATED,
        kind="medication",
        subject_id=payload.subject_id,
        record_id=medication.id,
    )
    db.commit()
    return _medication_out(service.reload_written(db, Medication, medication.id))


@router.patch(
    "/medications/{record_id}",
    response_model=schemas.MedicationOut,
    dependencies=[Depends(require(Action.WRITE, Module.HEALTH))],
    summary="Change a medication",
)
def update_medication(
    record_id: UUID,
    payload: schemas.MedicationPatch,
    db: DbDep,
    auth: AuthDep,
    client_ip: ClientIpDep,
) -> schemas.MedicationOut:
    medication = _get(db, auth, Medication, record_id)
    _require_subject(db, auth, medication.owner_id)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(medication, field, value)
    db.flush()

    _note(
        db,
        auth,
        client_ip,
        AuditAction.HEALTH_RECORD_UPDATED,
        kind="medication",
        subject_id=medication.owner_id,
        record_id=record_id,
    )
    db.commit()
    return _medication_out(medication)


@router.post(
    "/medications/{record_id}/doses",
    response_model=schemas.MedicationOut,
    dependencies=[Depends(require(Action.WRITE, Module.HEALTH))],
    summary="Log a dose taken",
)
def add_dose(
    record_id: UUID,
    payload: schemas.DoseIn,
    db: DbDep,
    auth: AuthDep,
    client_ip: ClientIpDep,
) -> schemas.MedicationOut:
    medication = _get(db, auth, Medication, record_id)
    _require_subject(db, auth, medication.owner_id)

    service.log_dose(
        db,
        medication,
        author_id=auth.user.id,
        taken_at=payload.taken_at or service.utcnow(),
        amount=payload.amount,
        note=payload.note,
    )
    _note(
        db,
        auth,
        client_ip,
        AuditAction.HEALTH_RECORD_UPDATED,
        kind="medication_dose",
        subject_id=medication.owner_id,
        record_id=record_id,
    )
    db.commit()
    db.expire(medication, ["doses"])
    return _medication_out(medication)


@router.delete(
    "/medications/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.HEALTH))],
    summary="Delete a medication and its dose log",
)
def delete_medication(
    record_id: UUID, db: DbDep, auth: AuthDep, client_ip: ClientIpDep
) -> Response:
    medication = _get(db, auth, Medication, record_id)
    _require_subject(db, auth, medication.owner_id)
    _note(
        db,
        auth,
        client_ip,
        AuditAction.HEALTH_RECORD_DELETED,
        kind="medication",
        subject_id=medication.owner_id,
        record_id=record_id,
    )
    service.delete_record(db, medication)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- lab results --------------------------------------------------------------


def _report_out(report: LabReport) -> schemas.LabReportOut:
    return schemas.LabReportOut(
        id=report.id,
        subject_id=report.owner_id,
        recorded_by_id=report.recorded_by_id,
        title=report.title,
        lab_name=report.lab_name,
        collected_on=report.collected_on,
        note=report.note,
        analytes=[
            schemas.AnalyteOut(
                id=row.id,
                name=row.name,
                value=row.value,
                text_value=row.text_value,
                unit=row.unit,
                reference_low=row.reference_low,
                reference_high=row.reference_high,
                reference_text=row.reference_text,
                position=row.position,
            )
            for row in report.analytes
        ],
    )


@router.get(
    "/lab-reports",
    response_model=list[schemas.LabReportOut],
    dependencies=[Depends(require(Action.READ, Module.HEALTH))],
    summary="Lab reports you can see",
)
def list_lab_reports(
    db: DbDep, auth: AuthDep, subject_id: UUID | None = None
) -> list[schemas.LabReportOut]:
    return [
        _report_out(report)
        for report in service.list_lab_reports(db, auth.principal, subject_id=subject_id)
    ]


@router.post(
    "/lab-reports",
    response_model=schemas.LabReportOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Action.WRITE, Module.HEALTH))],
    summary="Record a lab report",
)
def add_lab_report(
    payload: schemas.LabReportIn, db: DbDep, auth: AuthDep, client_ip: ClientIpDep
) -> schemas.LabReportOut:
    _require_subject(db, auth, payload.subject_id)
    report = service.create(
        db,
        LabReport,
        subject_id=payload.subject_id,
        author_id=auth.user.id,
        title=payload.title,
        lab_name=payload.lab_name,
        collected_on=payload.collected_on,
        note=payload.note,
    )
    for position, analyte in enumerate(payload.analytes):
        db.add(
            LabAnalyte(
                report_id=report.id,
                position=position,
                **analyte.model_dump(),
            )
        )
    db.flush()
    db.expire(report, ["analytes"])

    _note(
        db,
        auth,
        client_ip,
        AuditAction.HEALTH_RECORD_CREATED,
        kind="lab_report",
        subject_id=payload.subject_id,
        record_id=report.id,
    )
    db.commit()
    return _report_out(service.reload_written(db, LabReport, report.id))


@router.delete(
    "/lab-reports/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.HEALTH))],
    summary="Delete a lab report and its results",
)
def delete_lab_report(
    record_id: UUID, db: DbDep, auth: AuthDep, client_ip: ClientIpDep
) -> Response:
    report = _get(db, auth, LabReport, record_id)
    _require_subject(db, auth, report.owner_id)
    _note(
        db,
        auth,
        client_ip,
        AuditAction.HEALTH_RECORD_DELETED,
        kind="lab_report",
        subject_id=report.owner_id,
        record_id=record_id,
    )
    service.delete_record(db, report)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- activity -----------------------------------------------------------------


def _activity_out(entry: ActivityEntry) -> schemas.ActivityOut:
    return schemas.ActivityOut(
        id=entry.id,
        subject_id=entry.owner_id,
        recorded_by_id=entry.recorded_by_id,
        kind=entry.kind,
        label=entry.label,
        happened_at=entry.happened_at,
        duration_minutes=entry.duration_minutes,
        distance_miles=entry.distance_miles,
        calories=entry.calories,
        note=entry.note,
    )


@router.get(
    "/activity",
    response_model=list[schemas.ActivityOut],
    dependencies=[Depends(require(Action.READ, Module.HEALTH))],
    summary="Activity you can see",
)
def list_activity(
    db: DbDep, auth: AuthDep, subject_id: UUID | None = None, since: dt.datetime | None = None
) -> list[schemas.ActivityOut]:
    return [
        _activity_out(entry)
        for entry in service.list_activity(db, auth.principal, subject_id=subject_id, since=since)
    ]


@router.post(
    "/activity",
    response_model=schemas.ActivityOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Action.WRITE, Module.HEALTH))],
    summary="Record an activity",
)
def add_activity(
    payload: schemas.ActivityIn, db: DbDep, auth: AuthDep, client_ip: ClientIpDep
) -> schemas.ActivityOut:
    _require_subject(db, auth, payload.subject_id)
    entry = service.create(
        db,
        ActivityEntry,
        subject_id=payload.subject_id,
        author_id=auth.user.id,
        **payload.model_dump(exclude={"subject_id"}),
    )
    _note(
        db,
        auth,
        client_ip,
        AuditAction.HEALTH_RECORD_CREATED,
        kind="activity",
        subject_id=payload.subject_id,
        record_id=entry.id,
    )
    db.commit()
    return _activity_out(service.reload_written(db, ActivityEntry, entry.id))


@router.delete(
    "/activity/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.HEALTH))],
    summary="Delete an activity",
)
def delete_activity(record_id: UUID, db: DbDep, auth: AuthDep, client_ip: ClientIpDep) -> Response:
    entry = _get(db, auth, ActivityEntry, record_id)
    _require_subject(db, auth, entry.owner_id)
    _note(
        db,
        auth,
        client_ip,
        AuditAction.HEALTH_RECORD_DELETED,
        kind="activity",
        subject_id=entry.owner_id,
        record_id=record_id,
    )
    service.delete_record(db, entry)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- export -------------------------------------------------------------------


@router.get(
    "/export",
    dependencies=[Depends(require(Action.READ, Module.HEALTH))],
    summary="One member's records as CSV",
    response_class=Response,
)
def export(db: DbDep, auth: AuthDep, client_ip: ClientIpDep, subject_id: UUID) -> Response:
    """§4.8 requires export to work. Scoped, so it can hold nothing new.

    Audited, because a file of somebody's health records leaving the app is the
    clearest example there is of an event §4.1 wants a row for.
    """
    body = service.export_csv(db, auth.principal, subject_id)
    audit.record(
        db,
        AuditAction.DATA_EXPORTED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="health",
        client_ip=client_ip,
        detail={"subject_id": str(subject_id), "for_self": subject_id == auth.user.id},
    )
    db.commit()
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="health.csv"'},
    )
