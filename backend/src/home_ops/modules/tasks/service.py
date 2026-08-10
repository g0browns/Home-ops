"""Task operations (SPEC §4.4).

Every read here goes through `scoping.visible`. That is not politeness — `Task`
is a visibility-bearing model, so the guard installed in Phase 1 raises on any
select that skips it. The guard is the reason this module cannot quietly leak
another member's private chores.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Select, delete, func, select
from sqlalchemy.orm import Session as DbSession

from home_ops.modules.tasks import recurrence
from home_ops.modules.tasks.models import (
    LIVE_STATUSES,
    MAX_SUBTASK_DEPTH,
    Task,
    TaskAssignment,
    TaskCategory,
    TaskStatus,
)
from home_ops.policy import Principal, Visibility
from home_ops.scoping import SCOPED_OPTION, visible


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class SubtaskDepthExceeded(ValueError):
    """SPEC §4.4 wants subtasks, not an outline tool."""


class TaskNotVisible(LookupError):
    """The task does not exist, or the caller may not see it.

    Deliberately one exception for both. Distinguishing them tells a caller that
    a private task exists, which is the thing per-item visibility is for.
    """


@dataclass(frozen=True)
class TaskFilters:
    status: tuple[str, ...] | None = None
    category_id: UUID | None = None
    assignee_id: UUID | None = None
    search: str | None = None
    include_subtasks: bool = False


# --- reads --------------------------------------------------------------------


def visible_tasks(principal: Principal) -> Select[Any]:
    """The base statement every task read starts from."""
    return visible(select(Task), Task, principal)


def list_tasks(db: DbSession, principal: Principal, filters: TaskFilters) -> list[Task]:
    stmt = visible_tasks(principal)

    if filters.status:
        stmt = stmt.where(Task.status.in_(filters.status))
    if filters.category_id is not None:
        stmt = stmt.where(Task.category_id == filters.category_id)
    if filters.assignee_id is not None:
        stmt = stmt.where(
            select(TaskAssignment.task_id)
            .where(
                TaskAssignment.task_id == Task.id,
                TaskAssignment.user_id == filters.assignee_id,
            )
            .exists()
        )
    if filters.search:
        # Simple containment rather than full-text: a task title is a handful of
        # words, and ILIKE keeps "bin" matching "Bin day" without a stemmer
        # deciding otherwise. Notes get real FTS, where it earns its keep.
        pattern = f"%{filters.search.strip()}%"
        stmt = stmt.where(Task.title.ilike(pattern) | Task.description.ilike(pattern))
    if not filters.include_subtasks:
        stmt = stmt.where(Task.parent_task_id.is_(None))

    # Kanban ordering: manual position first, then soonest due, then newest.
    stmt = stmt.order_by(Task.position, Task.due_at.nulls_last(), Task.created_at.desc())
    return list(db.scalars(stmt))


def get_task(db: DbSession, principal: Principal, task_id: UUID) -> Task:
    task: Task | None = db.scalar(visible_tasks(principal).where(Task.id == task_id))
    if task is None:
        raise TaskNotVisible(str(task_id))
    return task


def subtasks_of(db: DbSession, principal: Principal, parent_id: UUID) -> list[Task]:
    stmt = visible_tasks(principal).where(Task.parent_task_id == parent_id)
    return list(db.scalars(stmt.order_by(Task.position, Task.created_at)))


def assignee_ids(db: DbSession, task_id: UUID) -> list[UUID]:
    return list(db.scalars(select(TaskAssignment.user_id).where(TaskAssignment.task_id == task_id)))


def assignees_for(db: DbSession, task_ids: list[UUID]) -> dict[UUID, list[UUID]]:
    """Assignees for many tasks at once, to keep list endpoints off N+1."""
    if not task_ids:
        return {}
    rows = db.execute(
        select(TaskAssignment.task_id, TaskAssignment.user_id).where(
            TaskAssignment.task_id.in_(task_ids)
        )
    )
    grouped: dict[UUID, list[UUID]] = {task_id: [] for task_id in task_ids}
    for task_id, user_id in rows:
        grouped[task_id].append(user_id)
    return grouped


# --- writes -------------------------------------------------------------------


def _depth_of(db: DbSession, task_id: UUID | None) -> int:
    """How deep a task sits. 1 for a top-level task."""
    depth = 1
    current = task_id
    while current is not None and depth <= MAX_SUBTASK_DEPTH + 1:
        # Exempt from the scoping guard, deliberately: this walks ancestry to
        # measure depth and returns an integer. It reads one foreign key and no
        # row content, and the caller has already proven it may edit the parent.
        parent = db.scalar(
            select(Task.parent_task_id)
            .where(Task.id == current)
            .execution_options(**{SCOPED_OPTION: True})
        )
        if parent is None:
            break
        depth += 1
        current = parent
    return depth


def create_task(
    db: DbSession,
    principal: Principal,
    *,
    title: str,
    description: str | None = None,
    category_id: UUID | None = None,
    priority: str = "none",
    due_at: dt.datetime | None = None,
    due_is_all_day: bool = True,
    parent_task_id: UUID | None = None,
    visibility: Visibility = Visibility.HOUSEHOLD,
    recurrence_rule: str | None = None,
    assignee_ids_: list[UUID] | None = None,
) -> Task:
    if parent_task_id is not None and _depth_of(db, parent_task_id) >= MAX_SUBTASK_DEPTH:
        raise SubtaskDepthExceeded(f"Subtasks nest {MAX_SUBTASK_DEPTH} levels deep at most.")

    rule = recurrence.validate_rule(recurrence_rule) if recurrence_rule else None

    task = Task(
        title=title.strip(),
        description=description,
        category_id=category_id,
        priority=priority,
        due_at=due_at,
        due_is_all_day=due_is_all_day,
        parent_task_id=parent_task_id,
        owner_id=principal.id,
        visibility=visibility.value,
        recurrence_rule=rule,
        # Every recurring task belongs to a series from the moment it exists, so
        # the one-live-instance index has something to hold on to.
        recurrence_group_id=uuid4() if rule else None,
        # The series anchor. Fixed now and never changed, so COUNT and UNTIL are
        # evaluated against the start of the series rather than the latest
        # instance — see recurrence.next_occurrence.
        recurrence_start_at=(due_at or utcnow()) if rule else None,
    )
    db.add(task)
    db.flush()

    if assignee_ids_:
        set_assignees(db, task, assignee_ids_)

    return task


def set_assignees(db: DbSession, task: Task, user_ids: list[UUID]) -> None:
    """Replace a task's assignees wholesale."""
    db.execute(delete(TaskAssignment).where(TaskAssignment.task_id == task.id))
    for user_id in dict.fromkeys(user_ids):  # de-duplicate, preserve order
        db.add(TaskAssignment(task_id=task.id, user_id=user_id))
    db.flush()


@dataclass(frozen=True)
class CompletionResult:
    completed: Task
    #: The next instance of a recurring series, when one was generated.
    successor: Task | None = None


def complete_task(db: DbSession, principal: Principal, task: Task) -> CompletionResult:
    """Mark a task done and, if it recurs, generate the next instance.

    The successor is created only after the current instance is marked done, so
    the one-live-instance index is never momentarily violated inside the
    transaction.
    """
    now = utcnow()
    task.status = TaskStatus.DONE.value
    task.completed_at = now
    task.completed_by_user_id = principal.id
    db.flush()

    if not task.recurrence_rule or task.recurrence_group_id is None:
        return CompletionResult(completed=task)

    following = recurrence.next_occurrence(
        task.recurrence_rule,
        series_start=task.recurrence_start_at or task.due_at or task.created_at,
        # A recurring task without a due date still needs an anchor; the moment
        # it was created is the only honest one.
        previous_due=task.due_at or task.created_at,
        now=now,
    )
    if following is None:
        # COUNT or UNTIL exhausted: the series is simply over.
        return CompletionResult(completed=task)

    successor = Task(
        title=task.title,
        description=task.description,
        category_id=task.category_id,
        priority=task.priority,
        due_at=following,
        due_is_all_day=task.due_is_all_day,
        parent_task_id=task.parent_task_id,
        owner_id=task.owner_id,
        visibility=task.visibility,
        recurrence_rule=task.recurrence_rule,
        recurrence_group_id=task.recurrence_group_id,
        # Carried unchanged: the successor belongs to the same series.
        recurrence_start_at=task.recurrence_start_at,
        position=task.position,
    )
    db.add(successor)
    db.flush()

    # Assignments carry forward: whoever does the bins keeps doing the bins
    # until someone says otherwise.
    for user_id in assignee_ids(db, task.id):
        db.add(TaskAssignment(task_id=successor.id, user_id=user_id))
    db.flush()

    return CompletionResult(completed=task, successor=successor)


def reopen_task(db: DbSession, task: Task) -> None:
    task.status = TaskStatus.OPEN.value
    task.completed_at = None
    task.completed_by_user_id = None
    db.flush()


def delete_task(db: DbSession, task: Task) -> None:
    """Deleting a parent takes its subtasks with it (ON DELETE CASCADE)."""
    db.delete(task)
    db.flush()


# --- categories ---------------------------------------------------------------


def list_categories(db: DbSession) -> list[TaskCategory]:
    """Not visibility-scoped: categories are household vocabulary, not content."""
    return list(db.scalars(select(TaskCategory).order_by(TaskCategory.position, TaskCategory.name)))


def category_in_use(db: DbSession, category_id: UUID) -> int:
    """How many tasks reference a category, for a meaningful delete warning.

    One of the few genuinely exempt reads, marked explicitly rather than left to
    trip the guard: it counts across every task regardless of visibility, but
    returns a single integer and never a row. Nothing about who owns those tasks
    or what they say can be recovered from a count, so this cannot be used to
    probe for private tasks.
    """
    return (
        db.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.category_id == category_id)
            .execution_options(**{SCOPED_OPTION: True})
        )
        or 0
    )


def live_statuses() -> tuple[str, ...]:
    return LIVE_STATUSES
