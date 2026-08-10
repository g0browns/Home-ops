"""Task and category endpoints (SPEC §4.4).

Both permission axes appear here, and they are not interchangeable:

* `require(Action.WRITE, Module.TASKS)` gates *the feature* — admins bypass it.
* `policy.can_edit_record` gates *the row* — nobody bypasses that, so a task you
  cannot see is one you cannot edit even as an admin.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from home_ops import audit
from home_ops.audit import AuditAction
from home_ops.dependencies import AuthDep, ClientIpDep, DbDep, require
from home_ops.modules.tasks import recurrence, schemas, service
from home_ops.modules.tasks.models import Task, TaskCategory, TaskStatus
from home_ops.policy import Action, Module, Visibility, can_edit_record

router = APIRouter(prefix="/tasks", tags=["tasks"])
categories_router = APIRouter(prefix="/task-categories", tags=["tasks"])


def _to_out(task: Task, assignees: list[UUID]) -> schemas.TaskOut:
    payload = schemas.TaskOut.model_validate(task)
    return payload.model_copy(
        update={
            "assignee_ids": assignees,
            "recurrence_label": (
                recurrence.describe(task.recurrence_rule) if task.recurrence_rule else None
            ),
        }
    )


def _load_editable(db: DbDep, auth: AuthDep, task_id: UUID, action: Action) -> Task:
    """Fetch a task the caller may see, then check they may act on it.

    Visibility first: a 404 for something you cannot see, a 403 for something
    you can see but may not change. Reversing that order would let a 403 confirm
    a private task exists.
    """
    try:
        task = service.get_task(db, auth.principal, task_id)
    except service.TaskNotVisible as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such task.") from exc

    permitted = can_edit_record(
        auth.principal,
        action,
        Module.TASKS,
        owner_id=task.owner_id,
        visibility=Visibility(task.visibility),
        assignee_ids=frozenset(service.assignee_ids(db, task.id)),
        deviations=auth.deviations,
    )
    if not permitted:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Not permitted to change this task.")
    return task


# --- categories ---------------------------------------------------------------


@categories_router.get(
    "",
    response_model=list[schemas.CategoryOut],
    dependencies=[Depends(require(Action.READ, Module.TASKS))],
    summary="List task categories",
)
def list_categories(db: DbDep) -> list[TaskCategory]:
    return service.list_categories(db)


@categories_router.post(
    "",
    response_model=schemas.CategoryOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Action.WRITE, Module.SETTINGS))],
    summary="Add a task category",
)
def create_category(
    payload: schemas.CategoryIn, auth: AuthDep, db: DbDep, client_ip: ClientIpDep
) -> TaskCategory:
    """Gated on settings, not tasks: categories are household vocabulary.

    Letting anyone who can tick a chore also invent categories is how a shared
    list turns into forty near-duplicates.
    """
    category = TaskCategory(
        name=payload.name.strip(), color_key=payload.color_key, position=payload.position
    )
    db.add(category)
    db.flush()
    audit.record(
        db,
        AuditAction.HOUSEHOLD_SETTING_CHANGED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="task_category",
        resource_id=str(category.id),
        client_ip=client_ip,
        detail={"created": category.name},
    )
    db.commit()
    return category


@categories_router.delete(
    "/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.SETTINGS))],
    summary="Remove a task category",
)
def delete_category(
    category_id: UUID, auth: AuthDep, db: DbDep, client_ip: ClientIpDep
) -> Response:
    category = db.get(TaskCategory, category_id)
    if category is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such category.")

    name = category.name
    # Tasks keep existing with category_id set to NULL (ON DELETE SET NULL) —
    # deleting a label must never delete the work filed under it.
    db.delete(category)
    audit.record(
        db,
        AuditAction.HOUSEHOLD_SETTING_CHANGED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="task_category",
        resource_id=str(category_id),
        client_ip=client_ip,
        detail={"deleted": name},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- tasks --------------------------------------------------------------------


@router.get(
    "",
    response_model=list[schemas.TaskOut],
    dependencies=[Depends(require(Action.READ, Module.TASKS))],
    summary="List tasks",
)
def list_tasks(
    auth: AuthDep,
    db: DbDep,
    status_filter: Annotated[list[TaskStatus] | None, Query(alias="status")] = None,
    category_id: UUID | None = None,
    assignee_id: UUID | None = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    include_subtasks: bool = False,
) -> list[schemas.TaskOut]:
    filters = service.TaskFilters(
        status=tuple(s.value for s in status_filter) if status_filter else None,
        category_id=category_id,
        assignee_id=assignee_id,
        search=search,
        include_subtasks=include_subtasks,
    )
    tasks = service.list_tasks(db, auth.principal, filters)
    assignees = service.assignees_for(db, [task.id for task in tasks])
    return [_to_out(task, assignees.get(task.id, [])) for task in tasks]


@router.post(
    "",
    response_model=schemas.TaskOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Action.WRITE, Module.TASKS))],
    summary="Create a task",
)
def create_task(
    payload: schemas.TaskCreate, auth: AuthDep, db: DbDep, client_ip: ClientIpDep
) -> schemas.TaskOut:
    if payload.parent_task_id is not None:
        # You may only hang a subtask off a task you can see and edit.
        _load_editable(db, auth, payload.parent_task_id, Action.WRITE)

    try:
        task = service.create_task(
            db,
            auth.principal,
            title=payload.title,
            description=payload.description,
            category_id=payload.category_id,
            priority=payload.priority.value,
            due_at=payload.due_at,
            due_is_all_day=payload.due_is_all_day,
            parent_task_id=payload.parent_task_id,
            visibility=payload.visibility,
            recurrence_rule=payload.recurrence_rule,
            assignee_ids_=payload.assignee_ids,
        )
    except recurrence.InvalidRecurrenceRule as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except service.SubtaskDepthExceeded as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    audit.record(
        db,
        AuditAction.TASK_CREATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="task",
        resource_id=str(task.id),
        client_ip=client_ip,
        detail={"title": task.title, "visibility": task.visibility},
    )
    db.commit()
    return _to_out(task, service.assignee_ids(db, task.id))


@router.get(
    "/{task_id}",
    response_model=schemas.TaskOut,
    dependencies=[Depends(require(Action.READ, Module.TASKS))],
    summary="Read one task",
)
def read_task(task_id: UUID, auth: AuthDep, db: DbDep) -> schemas.TaskOut:
    try:
        task = service.get_task(db, auth.principal, task_id)
    except service.TaskNotVisible as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such task.") from exc
    return _to_out(task, service.assignee_ids(db, task.id))


@router.patch(
    "/{task_id}",
    response_model=schemas.TaskOut,
    dependencies=[Depends(require(Action.WRITE, Module.TASKS))],
    summary="Update a task",
)
def update_task(
    task_id: UUID,
    payload: schemas.TaskUpdate,
    auth: AuthDep,
    db: DbDep,
    client_ip: ClientIpDep,
) -> schemas.TaskOut:
    task = _load_editable(db, auth, task_id, Action.WRITE)
    changes: dict[str, object] = {}

    simple_fields = {
        "title": payload.title,
        "description": payload.description,
        "category_id": payload.category_id,
        "due_at": payload.due_at,
        "due_is_all_day": payload.due_is_all_day,
        "position": payload.position,
    }
    for field, value in simple_fields.items():
        if value is not None:
            setattr(task, field, value)
            changes[field] = str(value)

    if payload.priority is not None:
        task.priority = payload.priority.value
        changes["priority"] = task.priority
    if payload.visibility is not None:
        task.visibility = payload.visibility.value
        changes["visibility"] = task.visibility

    if payload.recurrence_rule is not None:
        try:
            task.recurrence_rule = recurrence.validate_rule(payload.recurrence_rule)
        except recurrence.InvalidRecurrenceRule as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        if task.recurrence_group_id is None:
            task.recurrence_group_id = uuid4()
        changes["recurrence_rule"] = task.recurrence_rule

    if payload.status is not None and payload.status.value != task.status:
        if payload.status is TaskStatus.DONE:
            # Route status->done through completion so recurrence still fires;
            # otherwise ticking a chore on the kanban board would silently end
            # the series.
            result = service.complete_task(db, auth.principal, task)
            changes["status"] = TaskStatus.DONE.value
            if result.successor is not None:
                changes["generated_successor"] = str(result.successor.id)
        elif task.status == TaskStatus.DONE.value:
            service.reopen_task(db, task)
            task.status = payload.status.value
            changes["status"] = task.status
        else:
            task.status = payload.status.value
            changes["status"] = task.status

    if payload.assignee_ids is not None:
        service.set_assignees(db, task, payload.assignee_ids)
        changes["assignees"] = [str(uid) for uid in payload.assignee_ids]

    db.flush()
    audit.record(
        db,
        AuditAction.TASK_UPDATED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="task",
        resource_id=str(task.id),
        client_ip=client_ip,
        detail=changes,
    )
    db.commit()
    return _to_out(task, service.assignee_ids(db, task.id))


@router.post(
    "/{task_id}/complete",
    response_model=schemas.CompletionOut,
    dependencies=[Depends(require(Action.WRITE, Module.TASKS))],
    summary="Complete a task, generating the next instance if it recurs",
)
def complete_task(
    task_id: UUID, auth: AuthDep, db: DbDep, client_ip: ClientIpDep
) -> schemas.CompletionOut:
    task = _load_editable(db, auth, task_id, Action.WRITE)
    result = service.complete_task(db, auth.principal, task)

    audit.record(
        db,
        AuditAction.TASK_COMPLETED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="task",
        resource_id=str(task.id),
        client_ip=client_ip,
        detail={
            "title": task.title,
            "recurring": task.recurrence_rule is not None,
            "successor": str(result.successor.id) if result.successor else None,
        },
    )
    db.commit()

    return schemas.CompletionOut(
        completed=_to_out(result.completed, service.assignee_ids(db, result.completed.id)),
        successor=(
            _to_out(result.successor, service.assignee_ids(db, result.successor.id))
            if result.successor
            else None
        ),
    )


@router.get(
    "/{task_id}/subtasks",
    response_model=list[schemas.TaskOut],
    dependencies=[Depends(require(Action.READ, Module.TASKS))],
    summary="List a task's subtasks",
)
def list_subtasks(task_id: UUID, auth: AuthDep, db: DbDep) -> list[schemas.TaskOut]:
    try:
        service.get_task(db, auth.principal, task_id)
    except service.TaskNotVisible as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such task.") from exc

    children = service.subtasks_of(db, auth.principal, task_id)
    assignees = service.assignees_for(db, [child.id for child in children])
    return [_to_out(child, assignees.get(child.id, [])) for child in children]


@router.delete(
    "/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Action.WRITE, Module.TASKS))],
    summary="Delete a task and its subtasks",
)
def delete_task(task_id: UUID, auth: AuthDep, db: DbDep, client_ip: ClientIpDep) -> Response:
    task = _load_editable(db, auth, task_id, Action.WRITE)
    title = task.title
    service.delete_task(db, task)

    # SPEC §4.1 names deletions as security-relevant.
    audit.record(
        db,
        AuditAction.TASK_DELETED,
        actor_id=auth.user.id,
        actor_label=auth.user.username,
        resource_type="task",
        resource_id=str(task_id),
        client_ip=client_ip,
        detail={"title": title},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["categories_router", "router"]
