// Editing a task after it exists (SPEC §4.4).
//
// The quick-add row covers the common case — a title and maybe a date. This is
// everything else: description, priority, category, assignees, visibility and
// recurrence. Kept as a separate panel rather than inline fields on every row,
// because the list is meant to be scanned, not filled in.

import { useState, type FormEvent } from 'react'

import {
  updateTask,
  type HouseholdMember,
  type Task,
  type TaskCategory,
  type TaskPriority,
  type Visibility,
} from '../api/client'
import { MemberMark } from '../components/MemberMark'
import { Modal } from '../components/Modal'

const PRIORITIES: TaskPriority[] = ['none', 'low', 'medium', 'high', 'urgent']

const VISIBILITIES: { value: Visibility; label: string; hint: string }[] = [
  { value: 'household', label: 'Everyone', hint: 'Anyone in the household can see it' },
  { value: 'assignees', label: 'Assignees only', hint: 'Only you and whoever it is assigned to' },
  { value: 'private', label: 'Only me', hint: 'Nobody else, including admins' },
]

/** `datetime-local` wants `YYYY-MM-DDTHH:mm` in local time, not an ISO string. */
function toLocalInput(iso: string | null): string {
  if (!iso) return ''
  const date = new Date(iso)
  const pad = (value: number) => String(value).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}

export function TaskEditor({
  task,
  members,
  categories,
  onCancel,
  onSaved,
}: {
  readonly task: Task
  readonly members: readonly HouseholdMember[]
  readonly categories: readonly TaskCategory[]
  readonly onCancel: () => void
  readonly onSaved: () => void
}) {
  const [assignees, setAssignees] = useState<string[]>(task.assignee_ids)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function toggleAssignee(id: string) {
    setAssignees((current) =>
      current.includes(id) ? current.filter((value) => value !== id) : [...current, id],
    )
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    const due = String(data.get('due_at') ?? '')

    setBusy(true)
    setError(null)

    const result = await updateTask(task.id, {
      title: String(data.get('title') ?? '').trim(),
      description: String(data.get('description') ?? '') || null,
      // `new Date(local)` reads the value as local time, which is what the field
      // meant; toISOString then hands the server a proper instant.
      due_at: due ? new Date(due).toISOString() : null,
      priority: String(data.get('priority') ?? 'none') as TaskPriority,
      category_id: String(data.get('category_id') ?? '') || null,
      visibility: String(data.get('visibility') ?? 'household') as Visibility,
      recurrence_rule: String(data.get('recurrence_rule') ?? '') || null,
      assignee_ids: assignees,
    })

    setBusy(false)
    if (result.ok) onSaved()
    else setError('Could not save those changes.')
  }

  return (
    <Modal title="Edit task" onClose={onCancel} wide labelledBy="task-editor-title">
      <form className="note-editor" onSubmit={save} aria-label={`Edit ${task.title}`}>
      <div className="editor-head">
        {task.recurrence_label && (
          <span className="badge" data-tone="accent">
            repeats {task.recurrence_label.toLowerCase()}
          </span>
        )}
      </div>

      <div className="field">
        <label htmlFor="task-title">Title</label>
        <input id="task-title" name="title" defaultValue={task.title} required autoFocus />
      </div>

      <div className="field">
        <label htmlFor="task-description">Notes</label>
        <textarea
          id="task-description"
          name="description"
          rows={3}
          defaultValue={task.description ?? ''}
        />
      </div>

      <div className="editor-row">
        <div className="field">
          <label htmlFor="task-due">Due</label>
          <input
            id="task-due"
            name="due_at"
            type="datetime-local"
            defaultValue={toLocalInput(task.due_at)}
          />
        </div>

        <div className="field">
          <label htmlFor="task-priority">Priority</label>
          <select id="task-priority" name="priority" defaultValue={task.priority}>
            {PRIORITIES.map((value) => (
              <option key={value} value={value}>
                {value === 'none' ? 'None' : value[0]?.toUpperCase() + value.slice(1)}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="task-category">Category</label>
          <select id="task-category" name="category_id" defaultValue={task.category_id ?? ''}>
            <option value="">No category</option>
            {categories.map((category) => (
              <option key={category.id} value={category.id}>
                {category.name}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="task-repeat">Repeats</label>
          <select
            id="task-repeat"
            name="recurrence_rule"
            defaultValue={task.recurrence_rule ?? ''}
          >
            <option value="">Once</option>
            <option value="FREQ=DAILY">Daily</option>
            <option value="FREQ=WEEKLY">Weekly</option>
            <option value="FREQ=MONTHLY">Monthly</option>
            <option value="FREQ=YEARLY">Yearly</option>
          </select>
        </div>
      </div>

      <fieldset className="field assignee-picker">
        <legend>Assigned to</legend>
        <div className="assignee-options">
          {members.map((member) => {
            const chosen = assignees.includes(member.id)
            return (
              <label key={member.id} className="assignee-option" data-chosen={chosen}>
                <input
                  type="checkbox"
                  checked={chosen}
                  onChange={() => toggleAssignee(member.id)}
                />
                <MemberMark member={member} size="sm" />
                <span>{member.display_name}</span>
              </label>
            )
          })}
        </div>
      </fieldset>

      <div className="field">
        <label htmlFor="task-visibility">Who can see this</label>
        <select id="task-visibility" name="visibility" defaultValue={task.visibility}>
          {VISIBILITIES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label} — {option.hint}
            </option>
          ))}
        </select>
      </div>

      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}

      <div className="editor-actions">
        <button type="submit" className="button" disabled={busy}>
          {busy ? 'Saving…' : 'Save task'}
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
      </form>
    </Modal>
  )
}
