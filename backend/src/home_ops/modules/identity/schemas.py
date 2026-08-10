"""Request and response shapes for identity endpoints.

Every field is validated at the API boundary (SPEC §4.1). Response models never
include `password_hash`, `token_hash`, or OIDC subjects — the shapes below are
the allowlist, so a new column on `User` cannot leak by accident.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from home_ops.policy import Access, Module, Role, SubjectType
from home_ops.security import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, normalize_password

Username = Annotated[str, Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")]
DisplayName = Annotated[str, Field(min_length=1, max_length=128)]
Password = Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)]

# A palette key, validated at the boundary against the same tuple the assignment
# logic uses. Not a hex: the colour differs per theme and lives in tokens.css.
MemberHue = Literal["clay", "forest", "ochre", "indigo", "plum", "teal"]
AvatarColor = Annotated[MemberHue | None, Field(default=None)]


class UserPublic(BaseModel):
    """A member as any authenticated household member may see them."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    display_name: str
    avatar_color: str | None
    role: Role
    is_active: bool
    created_at: dt.datetime


class CurrentUser(UserPublic):
    """The caller's own record, plus what they are allowed to do.

    The permission map is included so the frontend can hide what it should hide.
    That is presentation only — every one of these is enforced server-side too,
    per SPEC §4.2's "enforced in the data layer, not by hiding UI".
    """

    permissions: dict[str, str]


class LoginRequest(BaseModel):
    username: Username
    # Not `Password`: the minimum length applies to setting a password, not to
    # attempting one. Rejecting a short login attempt early would leak the policy
    # and skip the rate limiter.
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class LoginResponse(BaseModel):
    user: CurrentUser
    csrf_token: str = Field(
        description="Echo this in the X-CSRF-Token header on state-changing requests."
    )


class SetupRequest(BaseModel):
    """Claim an unconfigured household. Available only while no user exists."""

    username: Username
    display_name: DisplayName
    password: Password


class SetupStatus(BaseModel):
    needs_setup: bool
    can_setup_here: bool = Field(
        description="False when setup is possible but not over this access path."
    )
    reason: str | None = None


class CreateUserRequest(BaseModel):
    username: Username
    display_name: DisplayName
    password: Password
    role: Role = Role.ADULT
    avatar_color: AvatarColor = None


class UpdateUserRequest(BaseModel):
    """Every field optional; only what is supplied changes."""

    display_name: DisplayName | None = None
    avatar_color: AvatarColor = None
    role: Role | None = None
    is_active: bool | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: Password

    @model_validator(mode="after")
    def _must_actually_change(self) -> ChangePasswordRequest:
        # Changing a password to itself revokes every session for no benefit,
        # which looks like a bug to whoever gets logged out.
        if normalize_password(self.current_password) == normalize_password(self.new_password):
            raise ValueError("New password must differ from the current one.")
        return self


class ResetPasswordRequest(BaseModel):
    """An administrator setting somebody else's password.

    No `current_password`, because whoever is doing this does not know it — that
    is the entire situation. What makes that acceptable is the two things the
    route insists on: it needs `users` write, and it refuses to act on the
    caller's own account, so it can never be the cheaper way to change your own
    password while holding a stolen session.
    """

    new_password: Password


class Belongings(BaseModel):
    """What deleting a member would destroy.

    Counted rather than described, because "are you sure?" is not a decision
    anybody can make and "this removes 34 tasks and 12 recipes" is.

    Health records are the exception: they are reported as a yes-or-no, never a
    number. §4.8 says an administrator gets no implicit access to another
    member's health data, and "Sam has 42 health records" is health data about
    Sam. Knowing the *category* exists is what the decision needs; the count
    adds nothing to it.
    """

    tasks: int = 0
    notes: int = 0
    recipes: int = 0
    events: int = 0
    contacts: int = 0
    shopping_lists: int = 0
    planned_meals: int = 0
    has_health_records: bool = False


class PermissionEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    subject_type: SubjectType
    subject_id: str
    module: Module
    access: Access


class SetPermissionRequest(BaseModel):
    subject_type: SubjectType
    subject_id: str = Field(min_length=1, max_length=64)
    module: Module
    access: Access


class AuditEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    at: dt.datetime
    actor_user_id: UUID | None
    actor_label: str | None
    action: str
    resource_type: str | None
    resource_id: str | None
    client_ip: str | None
    detail: dict[str, object]
