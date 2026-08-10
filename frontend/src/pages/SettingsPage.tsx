// Settings (SPEC §4.9). Household settings and personal ones, cleanly
// separated, with each control shown only when the caller may change it — and
// the server enforcing that regardless.

import { useCallback, useEffect, useState } from 'react'

import {
  createCalendar,
  createCategory,
  deleteCalendar,
  deleteCategory,
  getHouseholdSettings,
  listCalendars,
  listCategories,
  updateCalendar,
  updateHouseholdSetting,
  type CalendarSummary,
  type CurrentUser,
  type TaskCategory,
} from '../api/client'
import { ApiTokens } from '../components/ApiTokens'
import { useTheme } from '../hooks/usePreferences'
import { CALENDAR_HUES, calendarHueVar } from '../lib/calendars'
import { isWeekStart, WEEK_STARTS, weekStartLabel, type WeekStart } from '../lib/dates'
import { THEME_PREFERENCES, themeLabel, type ThemePreference } from '../lib/theme'

export function SettingsPage({ me }: { readonly me: CurrentUser }) {
  const [categories, setCategories] = useState<TaskCategory[]>([])
  const [calendars, setCalendars] = useState<CalendarSummary[]>([])
  const [householdName, setHouseholdName] = useState('')
  // Not a default of 'monday' in a second place: the server's registry owns the
  // default, and guessing it here would draw the wrong segment as selected for
  // as long as the fetch takes.
  const [weekStart, setWeekStart] = useState<WeekStart | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  const { preference, choose } = useTheme(true)

  const canManageHousehold = me.permissions['settings'] === 'write'

  const refresh = useCallback(async () => {
    const [categoryResult, calendarResult, settingsResult] = await Promise.all([
      listCategories(),
      listCalendars(),
      getHouseholdSettings(),
    ])
    if (categoryResult.ok) setCategories(categoryResult.data)
    if (calendarResult.ok) setCalendars(calendarResult.data)
    if (settingsResult.ok) {
      const value = settingsResult.data.values['household_name']
      setHouseholdName(typeof value === 'string' ? value : '')
      const firstDay = settingsResult.data.values['week_starts_on']
      if (isWeekStart(firstDay)) setWeekStart(firstDay)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  async function saveHouseholdName(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const result = await updateHouseholdSetting('household_name', householdName.trim())
    setSaved(result.ok ? 'Household name saved.' : null)
    await refresh()
  }

  /** Applied on click, with no Save button.
   *
   *  A three-way choice has nothing to type and nothing to get half-finished,
   *  so a Save button beside it only adds a step somebody can forget. The
   *  optimistic set is what makes the segment move under the finger; `refresh`
   *  then confirms it against what the server actually stored, so a refused
   *  write does not leave the wrong day looking chosen.
   */
  async function chooseWeekStart(value: WeekStart) {
    if (value === weekStart) return
    setWeekStart(value)
    const result = await updateHouseholdSetting('week_starts_on', value)
    setSaved(result.ok ? `Weeks now start on ${weekStartLabel(value)}.` : null)
    await refresh()
  }

  async function addCategory(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const name = String(new FormData(form).get('name') ?? '').trim()
    if (!name) return
    await createCategory({ name })
    form.reset()
    await refresh()
  }

  async function addCalendar(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const name = String(new FormData(form).get('name') ?? '').trim()
    if (!name) return
    // No colour sent: the server round-robins one, so a new calendar never
    // arrives the same colour as an existing one.
    await createCalendar({ name })
    form.reset()
    await refresh()
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>Settings</h1>
      </div>

      <section className="settings-section">
        <h2>Yours</h2>
        <p className="page-intro">
          Personal preferences. They follow your account, so they are the same
          whichever address you reach the app on.
        </p>

        <div className="field">
          <span className="field-label">Theme</span>
          <div className="segmented" role="group" aria-label="Theme">
            {THEME_PREFERENCES.map((option: ThemePreference) => (
              <button
                key={option}
                type="button"
                aria-pressed={preference === option}
                onClick={() => choose(option)}
              >
                {themeLabel(option)}
              </button>
            ))}
          </div>
        </div>
      </section>

      <section className="settings-section">
        <h2>Household</h2>
        {!canManageHousehold && (
          <p className="page-intro">
            These are shared settings. You can see them; an admin changes them.
          </p>
        )}

        <form onSubmit={saveHouseholdName} className="settings-row">
          <div className="field">
            <label htmlFor="household-name">Household name</label>
            <input
              id="household-name"
              value={householdName}
              onChange={(event) => setHouseholdName(event.currentTarget.value)}
              disabled={!canManageHousehold}
            />
          </div>
          {canManageHousehold && (
            <button type="submit" className="button">
              Save
            </button>
          )}
        </form>

        <div className="field">
          <span className="field-label" id="week-start-label">
            Weeks start on
          </span>
          <div className="segmented" role="group" aria-labelledby="week-start-label">
            {WEEK_STARTS.map((option) => (
              <button
                key={option}
                type="button"
                // Nothing is pressed until the fetch lands, rather than Monday
                // being pressed and then jumping.
                aria-pressed={weekStart === option}
                disabled={!canManageHousehold || weekStart === null}
                onClick={() => void chooseWeekStart(option)}
              >
                {weekStartLabel(option)}
              </button>
            ))}
          </div>
          <p className="field-hint">
            The first column of the month grid, and the week the meal planner and
            the shopping list build from.
          </p>
        </div>

        {saved && (
          <p className="notice" role="status">
            {saved}
          </p>
        )}
      </section>

      <section className="settings-section">
        <h2>Calendars</h2>
        <p className="page-intro">
          A calendar is a topic, and its colour fills the event on the month
          grid. Removing one <strong>takes its events with it</strong>, which is
          the one place this differs from task categories.
        </p>

        {calendars.length === 0 ? (
          <p className="empty">No calendars yet.</p>
        ) : (
          <ul className="calendar-list">
            {calendars.map((calendar) => (
              <li key={calendar.id}>
                <span
                  className="key-swatch"
                  style={{ background: calendarHueVar(calendar.color_key) }}
                  aria-hidden="true"
                />
                <span className="calendar-name">{calendar.name}</span>

                {canManageHousehold && (
                  <>
                    <label className="visually-hidden" htmlFor={`hue-${calendar.id}`}>
                      Colour for {calendar.name}
                    </label>
                    <select
                      id={`hue-${calendar.id}`}
                      value={calendar.color_key ?? ''}
                      onChange={async (event) => {
                        await updateCalendar(calendar.id, {
                          color_key: event.currentTarget.value || null,
                        })
                        await refresh()
                      }}
                    >
                      <option value="">No colour</option>
                      {CALENDAR_HUES.map((hue) => (
                        <option key={hue} value={hue}>
                          {hue}
                        </option>
                      ))}
                    </select>

                    {calendar.is_default ? (
                      <span className="badge">default</span>
                    ) : (
                      <>
                        <button
                          type="button"
                          className="link-button"
                          onClick={async () => {
                            await updateCalendar(calendar.id, { is_default: true })
                            await refresh()
                          }}
                        >
                          Make default
                          <span className="visually-hidden"> calendar: {calendar.name}</span>
                        </button>
                        <button
                          type="button"
                          className="link-button"
                          onClick={async () => {
                            await deleteCalendar(calendar.id)
                            await refresh()
                          }}
                        >
                          Remove
                          <span className="visually-hidden"> calendar {calendar.name} and its events</span>
                        </button>
                      </>
                    )}
                  </>
                )}
              </li>
            ))}
          </ul>
        )}

        {canManageHousehold && (
          <form onSubmit={addCalendar} className="settings-row">
            <div className="field">
              <label htmlFor="calendar-name">New calendar</label>
              <input id="calendar-name" name="name" placeholder="Work" required />
            </div>
            <button type="submit" className="button">
              Add
            </button>
          </form>
        )}
      </section>

      <section className="settings-section">
        <h2>Task categories</h2>
        <p className="page-intro">
          Shared vocabulary for filing tasks. Deleting one leaves its tasks in
          place, simply uncategorised.
        </p>

        {categories.length === 0 ? (
          <p className="empty">No categories yet.</p>
        ) : (
          <ul className="chip-list">
            {categories.map((category) => (
              <li key={category.id}>
                <span className="badge">{category.name}</span>
                {canManageHousehold && (
                  <button
                    type="button"
                    className="link-button"
                    onClick={async () => {
                      await deleteCategory(category.id)
                      await refresh()
                    }}
                  >
                    Remove
                    <span className="visually-hidden"> category {category.name}</span>
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}

        {canManageHousehold && (
          <form onSubmit={addCategory} className="settings-row">
            <div className="field">
              <label htmlFor="category-name">New category</label>
              <input id="category-name" name="name" placeholder="Chores" required />
            </div>
            <button type="submit" className="button">
              Add
            </button>
          </form>
        )}
      </section>

      {/* Last on the page, deliberately. It is the one section whose height is
          unbounded — a token per device, kept after revocation — so anywhere
          else it pushes the household settings below the fold and the short,
          frequently-changed things become the hard ones to reach. */}
      <ApiTokens me={me} />
    </div>
  )
}
