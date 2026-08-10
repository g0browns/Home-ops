// One calendar for picking one day, or a run of them (SPEC §4.3).
//
// **It replaces two `datetime-local` inputs**, which is the point. To put an
// event on a single day you previously had to type that day twice, once in
// "Starts" and once in "Ends", and getting them different by accident was the
// normal outcome. A month you can point at removes the second field entirely.
//
// Deliberately not a `<input type="date">` pair either: a date input cannot show
// a range, and the thing being chosen here is usually "these five days".
//
// Built on the same `monthGrid` the calendar page draws itself with, so the
// columns land on the household's week start and the six-row height does not
// jump between months.

import { useState } from 'react'

import { addMonths, isSameDay, isSameMonth, isoDate, monthGrid, startOfDay, weekdayLabels, type WeekStart } from '../lib/dates'
import { dayState, nextRange, type DayRange } from '../lib/dayRange'

export function DayRangePicker({
  range,
  weekStart,
  onChange,
  labelledBy,
}: {
  readonly range: DayRange
  readonly weekStart: WeekStart
  readonly onChange: (range: DayRange) => void
  readonly labelledBy?: string
}) {
  // Opens on the month holding the start of the selection, and is then the
  // user's to move. Not derived from `range` on every render: paging to December
  // to look, then clicking nothing, must not snap back to August.
  const [month, setMonth] = useState(() => startOfDay(new Date(`${range.from}T00:00:00`)))

  const days = monthGrid(month, weekStart)
  const today = startOfDay(new Date())

  return (
    <div className="day-picker" role="group" aria-labelledby={labelledBy}>
      <div className="day-picker-nav">
        <button
          type="button"
          onClick={() => setMonth(addMonths(month, -1))}
          aria-label="Previous month"
        >
          ‹
        </button>
        <strong>
          {month.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })}
        </strong>
        <button type="button" onClick={() => setMonth(addMonths(month, 1))} aria-label="Next month">
          ›
        </button>
      </div>

      <div className="day-picker-weekdays" aria-hidden="true">
        {weekdayLabels(weekStart).map((label) => (
          <span key={label}>{label}</span>
        ))}
      </div>

      <div className="day-picker-grid">
        {days.map((day) => {
          const iso = isoDate(day)
          const state = dayState(iso, range)
          return (
            <button
              key={iso}
              type="button"
              className="day-picker-day"
              data-state={state}
              data-outside={isSameMonth(day, month) ? undefined : ''}
              data-today={isSameDay(day, today) ? '' : undefined}
              // The selected days are the answer to one question, so they are one
              // pressed group rather than nine separate toggles.
              aria-pressed={state !== 'none'}
              // The date in full, because "16" read aloud in a grid of numbers
              // says nothing about which month it belongs to.
              aria-label={day.toLocaleDateString(undefined, {
                weekday: 'long',
                day: 'numeric',
                month: 'long',
                year: 'numeric',
              })}
              onClick={() => {
                const next = nextRange(range, iso)
                onChange(next)
                // Follow the selection if the click was on a day bleeding in from
                // a neighbouring month, so the grid does not appear to jump.
                if (!isSameMonth(day, month)) setMonth(startOfDay(day))
              }}
            >
              <span className="tabular">{day.getDate()}</span>
            </button>
          )
        })}
      </div>

      <p className="field-hint">
        Click a day. Click a second to make it a range — anything longer than one
        day is an all-day event.
      </p>
    </div>
  )
}
