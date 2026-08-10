"""What a setting *is* (SPEC §4.9).

The tables store keys and JSON values; this registry says which keys exist, what
type each holds, its default, and — for household settings — the permission
needed to change it. A key with no entry here is rejected on write, so the
settings tables cannot silently accumulate junk or typos.

Keeping defaults in code rather than as seeded rows means a fresh install and an
upgraded one behave identically, and a migration is never needed to add a
setting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from home_ops.policy import Action, Module

SettingType = Literal["bool", "int", "string"]


@dataclass(frozen=True)
class SettingSpec:
    key: str
    type: SettingType
    default: Any
    description: str
    # Household settings only: what it takes to change this one. Per SPEC §4.9,
    # "every setting gated by the permission level required to change it".
    write_action: Action = Action.WRITE
    write_module: Module = Module.SETTINGS
    choices: tuple[Any, ...] | None = None


HOUSEHOLD_SETTINGS: dict[str, SettingSpec] = {
    spec.key: spec
    for spec in (
        SettingSpec(
            key="household_name",
            type="string",
            default="Home",
            description="Shown in the header and in notification subject lines.",
        ),
        SettingSpec(
            key="week_starts_on",
            type="string",
            default="monday",
            choices=("monday", "sunday", "saturday"),
            description="First day of the week in calendar views (SPEC §4.3).",
        ),
        SettingSpec(
            key="mobile_app_url",
            type="string",
            default="",
            description=(
                "Where the phone app is served, so Settings can point people at it. "
                "Configuration rather than a constant: the app is a separate "
                "deployment on its own hostname, and only whoever set up the tunnel "
                "knows where it landed. Empty means it has not been deployed."
            ),
        ),
    )
}

USER_SETTINGS: dict[str, SettingSpec] = {
    spec.key: spec
    for spec in (
        SettingSpec(
            key="theme",
            type="string",
            default="system",
            choices=("system", "light", "dark"),
            description="Light or dark mode, or follow the operating system (SPEC §6).",
        ),
        SettingSpec(
            key="sidebar_collapsed",
            type="bool",
            default=False,
            description="Whether the left navigation is collapsed; persisted per SPEC §6.",
        ),
    )
}


class UnknownSettingError(KeyError):
    pass


class InvalidSettingValueError(ValueError):
    pass


def _validate(spec: SettingSpec, value: Any) -> Any:
    # bool before int: in Python `isinstance(True, int)` is True, so checking
    # int first would quietly accept a boolean for an integer setting.
    if spec.type == "bool":
        if not isinstance(value, bool):
            raise InvalidSettingValueError(f"{spec.key} must be a boolean.")
    elif spec.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidSettingValueError(f"{spec.key} must be an integer.")
    elif spec.type == "string":
        if not isinstance(value, str):
            raise InvalidSettingValueError(f"{spec.key} must be a string.")
        if len(value) > 200:
            raise InvalidSettingValueError(f"{spec.key} must be 200 characters or fewer.")

    if spec.choices is not None and value not in spec.choices:
        allowed = ", ".join(str(choice) for choice in spec.choices)
        raise InvalidSettingValueError(f"{spec.key} must be one of: {allowed}.")

    return value


def validate_household_setting(key: str, value: Any) -> Any:
    spec = HOUSEHOLD_SETTINGS.get(key)
    if spec is None:
        raise UnknownSettingError(key)
    return _validate(spec, value)


def validate_user_setting(key: str, value: Any) -> Any:
    spec = USER_SETTINGS.get(key)
    if spec is None:
        raise UnknownSettingError(key)
    return _validate(spec, value)


def household_defaults() -> dict[str, Any]:
    return {key: spec.default for key, spec in HOUSEHOLD_SETTINGS.items()}


def user_defaults() -> dict[str, Any]:
    return {key: spec.default for key, spec in USER_SETTINGS.items()}
