// What an occurrence covers, in calendar days (SPEC §4.3).
//
// Pure, and separate from the page because of one trap that is invisible until
// it is wrong: **an all-day event stores an exclusive end.** A single all-day
// event on the 3rd ends at midnight on the 4th, so counted naively every
// all-day event looks like it spans two days — which would put every one of
// them in the agenda's "across several days" section and make that section
// useless. Subtracting a millisecond before taking the day is the whole fix,
// and it is the reason these live somewhere with tests.

import type { Occurrence } from '../api/client'
import { startOfDay } from './dates'

/** The last calendar day an occurrence actually touches. */
function lastDay(occurrence: Occurrence): Date {
  const rawEnd = new Date(occurrence.ends_at)
  return startOfDay(occurrence.is_all_day ? new Date(rawEnd.getTime() - 1) : rawEnd)
}

/** Whole days covered, counted in local calendar days. Never less than one. */
export function daysCovered(occurrence: Occurrence): number {
  const start = startOfDay(new Date(occurrence.starts_at)).getTime()
  const end = lastDay(occurrence).getTime()
  return Math.max(1, Math.round((end - start) / 86_400_000) + 1)
}

/** More than one day, which is what earns an event its own agenda section. */
export function isMultiDay(occurrence: Occurrence): boolean {
  return daysCovered(occurrence) > 1
}

/** Does this occurrence touch the given calendar day at all? */
export function coversDay(occurrence: Occurrence, day: Date): boolean {
  const from = startOfDay(new Date(occurrence.starts_at)).getTime()
  const to = lastDay(occurrence).getTime()
  const at = startOfDay(day).getTime()
  return at >= from && at <= to
}

/** "Aug 3 – Aug 7", the span an event actually covers. */
export function rangeLabel(occurrence: Occurrence): string {
  const short: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric' }
  const start = new Date(occurrence.starts_at).toLocaleDateString(undefined, short)
  const end = lastDay(occurrence).toLocaleDateString(undefined, short)
  return `${start} – ${end}`
}
