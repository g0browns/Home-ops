// Tasks (SPEC §4.4): a list view and a kanban board over the same data.
//
// Both views use the Rota devices established in Phase 2 — a member's colour on
// the leading edge, banded rows, a word beside every colour — so the module
// reads as part of the app rather than as a bolt-on.

import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  completeTask,
  createTask,
  deleteTask,
  listCategories,
  listTasks,
  listUsers,
  updateTask,
  type CurrentUser,
  type HouseholdMember,
  type Task,
  type TaskCategory,
  type TaskStatus,
} from '../api/client'
import { MemberBarCell, MemberChip, MemberMark } from '../components/MemberMark'
import { Icon } from '../components/icons'
import { hueVar } from '../lib/members'
import { TaskEditor } from './TaskEditor'

const BOARD_COLUMNS: { readonly status: TaskStatus; readonly label: string }[] = [
  { status: 'open', label: 'To do' },
  { status: 'in_progress', label: 'In progress' },
  { status: 'done', label: 'Done' },
]

const PRIORITY_TONE: Record<string, string> = {
  urgent: 'danger',
  high: 'danger',
  medium: 'accent',
  low: 'default',
  none: 'default',
}

/**
 * Whose colour the row's leading bar shows.
 *
 * The **assignee**, not the owner. The whole point of the Rota direction is
 * finding your own rows by colour before reading them, and on a task that means
 * whoever has to do it — not whoever typed it in. Keying this to the owner made
 * every row the same colour the moment one person added them all, which is
 * exactly what a shared household list looks like.
 *
 * Falls back to the owner for an unassigned task, so the bar still says
 * something rather than going blank.
 */
function barMember(
  task: Task,
  byId: Map<string, HouseholdMember>,
): HouseholdMember | undefined {
  const firstAssignee = task.assignee_ids[0]
  return (firstAssignee ? byId.get(firstAssignee) : undefined) ?? byId.get(task.owner_id)
}

function dueLabel(task: Task): { text: string; overdue: boolean } {
  if (!task.due_at) return { text: '—', overdue: false }

  const due = new Date(task.due_at)
  const now = new Date()
  const overdue = due < now && task.status !== 'done'
  const sameYear = due.getFullYear() === now.getFullYear()

  const text = due.toLocaleDateString(undefined, {
    day: 'numeric',
    month: 'short',
    ...(sameYear ? {} : { year: 'numeric' }),
  })
  return { text, overdue }
}

export function TasksPage({ me }: { readonly me: CurrentUser }) {
  const [tasks, setTasks] = useState<Task[] | null>(null)
  const [members, setMembers] = useState<HouseholdMember[]>([])
  const [categories, setCategories] = useState<TaskCategory[]>([])
  const [view, setView] = useState<'list' | 'board'>('list')
  const [mineOnly, setMineOnly] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [editing, setEditing] = useState<Task | null>(null)
  // The card being dragged, so a column knows what it is about to receive.
  const [dragging, setDragging] = useState<Task | null>(null)

  const canWrite = me.permissions['tasks'] === 'write'

  const refresh = useCallback(async () => {
    const [taskResult, memberResult, categoryResult] = await Promise.all([
      listTasks(mineOnly ? { assignee_id: me.id } : {}),
      listUsers(),
      listCategories(),
    ])
    if (taskResult.ok) setTasks(taskResult.data)
    if (memberResult.ok) setMembers(memberResult.data)
    if (categoryResult.ok) setCategories(categoryResult.data)
  }, [mineOnly, me.id])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const byId = useMemo(
    () => new Map(members.map((member) => [member.id, member])),
    [members],
  )
  const categoryById = useMemo(
    () => new Map(categories.map((category) => [category.id, category])),
    [categories],
  )

  async function add(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    const title = String(data.get('title') ?? '').trim()
    if (!title) return

    setBusy(true)
    const due = String(data.get('due') ?? '')
    const result = await createTask({
      title,
      due_at: due ? new Date(`${due}T09:00`).toISOString() : null,
      recurrence_rule: String(data.get('repeat') ?? '') || null,
      category_id: String(data.get('category') ?? '') || null,
    })
    setBusy(false)

    if (result.ok) {
      form.reset()
      setMessage(null)
      await refresh()
    } else {
      setMessage('Could not add that task.')
    }
  }

  async function markDone(task: Task) {
    const result = await completeTask(task.id)
    if (result.ok && result.data.successor) {
      const next = dueLabel(result.data.successor)
      // The visible consequence of "one open instance at a time": say where the
      // next one went, rather than letting it appear silently.
      setMessage(`“${task.title}” done. Next one due ${next.text}.`)
    } else {
      setMessage(null)
    }
    await refresh()
  }

  async function move(task: Task, status: TaskStatus) {
    await updateTask(task.id, { status })
    await refresh()
  }

  async function remove(task: Task) {
    await deleteTask(task.id)
    await refresh()
  }

  if (tasks === null) return <p className="loading">Loading tasks…</p>

  const open = tasks.filter((task) => task.status === 'open' || task.status === 'in_progress')
  const overdue = open.filter((task) => dueLabel(task).overdue)

  return (
    <div className="page">
      <div className="page-head">
        <h1>Tasks</h1>
        <p className="page-summary">
          <strong>{open.length}</strong> open
          {overdue.length > 0 && (
            <>
              {' '}
              · <strong className="overdue">{overdue.length}</strong> overdue
            </>
          )}
        </p>
      </div>

      <div className="toolbar">
        <div className="segmented" role="group" aria-label="View">
          {(['list', 'board'] as const).map((option) => (
            <button
              key={option}
              type="button"
              aria-pressed={view === option}
              onClick={() => setView(option)}
            >
              {option === 'list' ? 'List' : 'Board'}
            </button>
          ))}
        </div>

        <label className="check">
          <input
            type="checkbox"
            checked={mineOnly}
            onChange={(event) => setMineOnly(event.currentTarget.checked)}
          />
          Assigned to me
        </label>
      </div>

      {canWrite && (
        <form className="quick-add" onSubmit={add}>
          <input name="title" placeholder="Add a task…" aria-label="Task title" required />
          <input name="due" type="date" aria-label="Due date" />
          <select name="repeat" aria-label="Repeat" defaultValue="">
            <option value="">Once</option>
            <option value="FREQ=DAILY">Daily</option>
            <option value="FREQ=WEEKLY">Weekly</option>
            <option value="FREQ=MONTHLY">Monthly</option>
          </select>
          <select name="category" aria-label="Category" defaultValue="">
            <option value="">No category</option>
            {categories.map((category) => (
              <option key={category.id} value={category.id}>
                {category.name}
              </option>
            ))}
          </select>
          <button type="submit" className="button" disabled={busy}>
            Add
          </button>
        </form>
      )}

      {message && (
        <p className="notice" role="status">
          {message}
        </p>
      )}

      {editing && (
        <TaskEditor
          task={editing}
          members={members}
          categories={categories}
          onCancel={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null)
            setMessage(null)
            await refresh()
          }}
        />
      )}

      {tasks.length === 0 ? (
        <p className="empty">
          Nothing here yet.{canWrite ? ' Add the first task above.' : ''}
        </p>
      ) : view === 'list' ? (
        <ListView
          tasks={tasks}
          byId={byId}
          categoryById={categoryById}
          canWrite={canWrite}
          onDone={markDone}
          onDelete={remove}
          onEdit={setEditing}
        />
      ) : (
        <BoardView
          tasks={tasks}
          byId={byId}
          canWrite={canWrite}
          onMove={move}
          onDone={markDone}
          onEdit={setEditing}
          dragging={dragging}
          onDragStart={setDragging}
          onDragEnd={() => setDragging(null)}
        />
      )}
    </div>
  )
}

function Assignees({
  ids,
  byId,
}: {
  readonly ids: string[]
  readonly byId: Map<string, HouseholdMember>
}) {
  const people = ids.map((id) => byId.get(id)).filter(Boolean) as HouseholdMember[]
  if (people.length === 0) return <span className="muted">Unassigned</span>
  return (
    <span className="assignees">
      {people.map((person) => (
        <MemberChip key={person.id} member={person} />
      ))}
    </span>
  )
}

function ListView({
  tasks,
  byId,
  categoryById,
  canWrite,
  onDone,
  onDelete,
  onEdit,
}: {
  readonly tasks: Task[]
  readonly byId: Map<string, HouseholdMember>
  readonly categoryById: Map<string, TaskCategory>
  readonly canWrite: boolean
  readonly onDone: (task: Task) => void
  readonly onDelete: (task: Task) => void
  readonly onEdit: (task: Task) => void
}) {
  return (
    <div className="table-frame">
      <table className="data">
        <caption className="visually-hidden">Tasks, their assignees and due dates</caption>
        <thead>
          <tr>
            <th className="lead">
              <span className="visually-hidden">Owner colour</span>
            </th>
            <th scope="col">Task</th>
            <th scope="col">Assigned</th>
            <th scope="col">Category</th>
            <th scope="col">Repeat</th>
            <th scope="col" className="num">
              Due
            </th>
            <th scope="col">Status</th>
            {canWrite && (
              <th scope="col">
                <span className="visually-hidden">Actions</span>
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {tasks.map((task) => {
            const marker = barMember(task, byId)
            const due = dueLabel(task)
            const category = task.category_id ? categoryById.get(task.category_id) : undefined
            return (
              <tr key={task.id} data-done={task.status === 'done'}>
                {marker ? (
                  <MemberBarCell member={marker} />
                ) : (
                  <td className="lead" aria-hidden="true" />
                )}
                <td>
                  <span className="cell-name">
                    {task.title}
                    {task.priority !== 'none' && (
                      <span className="badge" data-tone={PRIORITY_TONE[task.priority]}>
                        {task.priority}
                      </span>
                    )}
                    {task.visibility === 'private' && (
                      <span className="badge" title="Only you can see this">
                        private
                      </span>
                    )}
                  </span>
                </td>
                <td>
                  <Assignees ids={task.assignee_ids} byId={byId} />
                </td>
                <td className="muted">{category?.name ?? '—'}</td>
                <td className="muted">{task.recurrence_label ?? '—'}</td>
                <td className={`num ${due.overdue ? 'overdue' : 'muted'}`}>{due.text}</td>
                <td>
                  <span className="badge" data-tone={task.status === 'done' ? 'positive' : 'default'}>
                    {task.status.replace('_', ' ')}
                  </span>
                </td>
                {canWrite && (
                  <td className="row-actions">
                    <button
                      type="button"
                      className="icon-button"
                      onClick={() => onEdit(task)}
                      title="Edit"
                    >
                      <Icon name="pencil" />
                      <span className="visually-hidden">Edit “{task.title}”</span>
                    </button>
                    {task.status !== 'done' && (
                      <button
                        type="button"
                        className="icon-button"
                        onClick={() => onDone(task)}
                        title="Mark done"
                      >
                        <Icon name="check" />
                        <span className="visually-hidden">Mark “{task.title}” done</span>
                      </button>
                    )}
                    <button
                      type="button"
                      className="icon-button"
                      onClick={() => onDelete(task)}
                      title="Delete"
                    >
                      <Icon name="trash" />
                      <span className="visually-hidden">Delete “{task.title}”</span>
                    </button>
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function BoardView({
  tasks,
  byId,
  canWrite,
  onMove,
  onDone,
  onEdit,
  dragging,
  onDragStart,
  onDragEnd,
}: {
  readonly tasks: Task[]
  readonly byId: Map<string, HouseholdMember>
  readonly canWrite: boolean
  readonly onMove: (task: Task, status: TaskStatus) => void
  readonly onDone: (task: Task) => void
  readonly onEdit: (task: Task) => void
  readonly dragging: Task | null
  readonly onDragStart: (task: Task) => void
  readonly onDragEnd: () => void
}) {
  const [over, setOver] = useState<TaskStatus | null>(null)

  /**
   * Resolve the dragged task from the drop event itself.
   *
   * The id travels in `dataTransfer`, which is what it is for, rather than
   * being read back out of React state set during `dragstart`. State is the
   * fragile choice: it is only guaranteed to be visible to the drop handler
   * after a re-render, so a fast drag — or any programmatic one — can arrive
   * before the component knows anything is being dragged. `dragging` remains
   * as the fallback and drives the visual affordances.
   */
  function taskFromDrop(event: React.DragEvent): Task | null {
    const id = event.dataTransfer.getData('text/plain')
    return tasks.find((task) => task.id === id) ?? dragging
  }

  function drop(event: React.DragEvent, status: TaskStatus) {
    setOver(null)
    onDragEnd()
    const task = taskFromDrop(event)
    if (!task || task.status === status) return
    onMove(task, status)
  }

  return (
    <div className="board">
      {BOARD_COLUMNS.map((column) => {
        const inColumn = tasks.filter((task) => task.status === column.status)
        const receiving = over === column.status && dragging !== null && dragging.status !== column.status
        return (
          <section
            key={column.status}
            className="board-column"
            aria-label={column.label}
            data-receiving={receiving}
            onDragOver={(event) => {
              if (!canWrite) return
              // preventDefault is what marks this a valid drop target; without
              // it the browser refuses the drop and the card springs back.
              event.preventDefault()
              event.dataTransfer.dropEffect = 'move'
              setOver(column.status)
            }}
            onDragLeave={(event) => {
              // Only clear when the pointer actually leaves the column, not when
              // it crosses between the cards inside it.
              if (!event.currentTarget.contains(event.relatedTarget as Node)) {
                setOver((current) => (current === column.status ? null : current))
              }
            }}
            onDrop={(event) => {
              event.preventDefault()
              drop(event, column.status)
            }}
          >
            <header className="board-column-head">
              <h2>{column.label}</h2>
              <span className="tabular muted">{inColumn.length}</span>
            </header>

            <ul className="board-cards">
              {inColumn.map((task) => {
                const marker = barMember(task, byId)
                const due = dueLabel(task)
                return (
                  <li
                    key={task.id}
                    className="card"
                    draggable={canWrite}
                    data-dragging={dragging?.id === task.id}
                    onDragStart={(event) => {
                      onDragStart(task)
                      event.dataTransfer.effectAllowed = 'move'
                      // Firefox will not start a drag without payload data.
                      event.dataTransfer.setData('text/plain', task.id)
                    }}
                    onDragEnd={onDragEnd}
                  >
                    <span
                      className="card-bar"
                      style={{ background: hueVar(marker?.avatar_color) }}
                      aria-hidden="true"
                    />
                    <div className="card-body">
                      <p className="card-title">{task.title}</p>
                      <div className="card-meta">
                        {task.assignee_ids.map((id) => {
                          const person = byId.get(id)
                          return person ? (
                            <MemberMark key={id} member={person} size="sm" />
                          ) : null
                        })}
                        {task.recurrence_label && (
                          <span className="badge">{task.recurrence_label}</span>
                        )}
                        <span className={`tabular ${due.overdue ? 'overdue' : 'muted'}`}>
                          {due.text}
                        </span>
                      </div>

                      {canWrite && (
                        <div className="card-actions">
                          {column.status !== 'open' && (
                            <button type="button" onClick={() => onMove(task, 'open')}>
                              To do
                            </button>
                          )}
                          {column.status !== 'in_progress' && task.status !== 'done' && (
                            <button type="button" onClick={() => onMove(task, 'in_progress')}>
                              Start
                            </button>
                          )}
                          {task.status !== 'done' && (
                            <button type="button" onClick={() => onDone(task)}>
                              Done
                            </button>
                          )}
                          <button type="button" onClick={() => onEdit(task)}>
                            Edit
                          </button>
                        </div>
                      )}
                    </div>
                  </li>
                )
              })}
              {inColumn.length === 0 && <li className="board-empty">Nothing here</li>}
            </ul>
          </section>
        )
      })}
    </div>
  )
}
