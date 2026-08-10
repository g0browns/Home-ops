"""Household and per-user settings endpoints (SPEC §4.9).

Two separations the spec asks for, kept visible in the URLs:

* `/api/settings/household` — shared, gated by the `settings` module permission.
* `/api/settings/me` — the caller's own, always writable by them regardless of
  role. A read-only member still chooses their own theme.

Every write is audit-logged.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from home_ops import audit
from home_ops.audit import AuditAction
from home_ops.dependencies import AuthDep, ClientIpDep, DbDep, require
from home_ops.modules.settings import registry
from home_ops.modules.settings.models import HouseholdSetting, UserSetting
from home_ops.policy import Action, Module

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingValue(BaseModel):
    value: Any = Field(description="Type is per the setting's registry entry.")


class SettingsDocument(BaseModel):
    """Defaults merged with whatever has been stored.

    Callers get a complete document, so the frontend never has to know a
    default. Adding a setting therefore needs no data migration.
    """

    values: dict[str, Any]


def _stored_household(db: DbDep) -> dict[str, Any]:
    return {row.key: row.value for row in db.scalars(select(HouseholdSetting))}


@router.get("/household", response_model=SettingsDocument, summary="Household settings")
def read_household_settings(
    db: DbDep,
    _: AuthDep,
) -> SettingsDocument:
    """Readable by any authenticated member.

    Reading these is not gated on `settings` module access: the household name
    and week-start affect what everyone sees, and hiding them would break the
    calendar for limited members rather than protect anything. Writing is gated.
    """
    return SettingsDocument(values={**registry.household_defaults(), **_stored_household(db)})


@router.put(
    "/household/{key}",
    response_model=SettingsDocument,
    dependencies=[Depends(require(Action.WRITE, Module.SETTINGS))],
    summary="Change a household setting",
)
def write_household_setting(
    key: str,
    payload: SettingValue,
    auth: AuthDep,
    db: DbDep,
    client_ip: ClientIpDep,
) -> SettingsDocument:
    try:
        value = registry.validate_household_setting(key, payload.value)
    except registry.UnknownSettingError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No such setting: {key}"
        ) from exc
    except registry.InvalidSettingValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    row = db.get(HouseholdSetting, key)
    previous = row.value if row is not None else registry.HOUSEHOLD_SETTINGS[key].default
    if row is None:
        row = HouseholdSetting(key=key, value=value, updated_by_user_id=auth.user.id)
        db.add(row)
    else:
        row.value = value
        row.updated_by_user_id = auth.user.id

    db.flush()
    audit.record(
        db,
        AuditAction.HOUSEHOLD_SETTING_CHANGED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="household_setting",
        resource_id=key,
        client_ip=client_ip,
        detail={"key": key, "from": previous, "to": value},
    )
    db.commit()
    return SettingsDocument(values={**registry.household_defaults(), **_stored_household(db)})


@router.get("/me", response_model=SettingsDocument, summary="Your own settings")
def read_my_settings(auth: AuthDep, db: DbDep) -> SettingsDocument:
    stored = {
        row.key: row.value
        for row in db.scalars(select(UserSetting).where(UserSetting.user_id == auth.user.id))
    }
    return SettingsDocument(values={**registry.user_defaults(), **stored})


@router.put("/me/{key}", response_model=SettingsDocument, summary="Change your own setting")
def write_my_setting(
    key: str,
    payload: SettingValue,
    auth: AuthDep,
    db: DbDep,
    client_ip: ClientIpDep,
) -> SettingsDocument:
    """No module permission required — these are yours.

    Scoped to `auth.user.id` throughout, so there is no path by which one member
    writes another's preferences.
    """
    try:
        value = registry.validate_user_setting(key, payload.value)
    except registry.UnknownSettingError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No such setting: {key}"
        ) from exc
    except registry.InvalidSettingValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    row = db.get(UserSetting, (auth.user.id, key))
    if row is None:
        row = UserSetting(user_id=auth.user.id, key=key, value=value)
        db.add(row)
    else:
        row.value = value

    db.flush()
    audit.record(
        db,
        AuditAction.USER_SETTING_CHANGED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="user_setting",
        resource_id=key,
        client_ip=client_ip,
        detail={"key": key, "to": value},
    )
    db.commit()

    stored = {
        r.key: r.value
        for r in db.scalars(select(UserSetting).where(UserSetting.user_id == auth.user.id))
    }
    return SettingsDocument(values={**registry.user_defaults(), **stored})
