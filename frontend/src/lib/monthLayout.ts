// Laying a week's events out as bars (SPEC §4.3).
//
// An event running Monday to Wednesday is one thing, not three, and a wall
// planner has to draw it as one continuous bar across those three columns —
// which is what every calendar people already use does, and what a chip
// repeated in each day cell fails to convey. Crossing into the next week
// restarts the bar on the next row, with its cut edges marked.
//
// Pure arithmetic over local `Date`s, deliberately: the server has already
// resolved every occurrence to an instant, and this is the part that is easy to
// get subtly wrong for one week a year.

import { addDays, overlapsDay, startOfDay } from './dates'

export interface LayoutEntry<T> {
  readonly item: T
  readonly start: Date
  readonly end: Date
  /**
   * Lower ranks take the lanes nearer the top. Events are 0 and task deadlines
   * are 1, so appointments sit above the things that are merely due.
   */
  readonly rank?: number
}

export interface Span<T> {
  readonly item: T
  /** First column this bar covers, 0-based within the week. */
  readonly startColumn: number
  /** Last column it covers, inclusive. */
  readonly endColumn: number
  /** Which row of bars it sits on. Stable across the columns it spans. */
  readonly lane: number
  /** It began before this week — the bar's leading edge is a cut, not a start. */
  readonly continuesBefore: boolean
  /** It runs past this week. */
  readonly continuesAfter: boolean
}

export interface WeekLayout<T> {
  readonly spans: readonly Span<T>[]
  readonly laneCount: number
}

/**
 * Place a week's entries into lanes.
 *
 * Longer bars are placed first so they settle at the top and single-day items
 * fill in beneath them, rather than a one-day chip stranding a three-day bar on
 * lane 4.
 */
export function layoutWeek<T>(
  week: readonly Date[],
  entries: readonly LayoutEntry<T>[],
): WeekLayout<T> {
  const first = week[0]
  const last = week[week.length - 1]
  if (!first || !last) return { spans: [], laneCount: 0 }

  const weekStart = startOfDay(first)
  const weekEnd = addDays(startOfDay(last), 1)

  const placed: Span<T>[] = []
  // One entry per lane, holding the columns already taken on that lane.
  const lanes: { start: number; end: number }[][] = []

  const candidates = entries
    .map((entry) => {
      // A zero-length entry still belongs to its day; without this the overlap
      // test below excludes it from every column.
      const end = entry.end > entry.start ? entry.end : new Date(entry.start.getTime() + 1)
      if (entry.start >= weekEnd || end <= weekStart) return null

      let startColumn = -1
      let endColumn = -1
      for (let column = 0; column < week.length; column += 1) {
        const day = week[column]
        if (day && overlapsDay(entry.start, end, day)) {
          if (startColumn === -1) startColumn = column
          endColumn = column
        }
      }
      if (startColumn === -1) return null

      return {
        entry,
        startColumn,
        endColumn,
        continuesBefore: entry.start < weekStart,
        continuesAfter: end > weekEnd,
      }
    })
    .filter((candidate) => candidate !== null)

  candidates.sort((a, b) => {
    const rank = (a.entry.rank ?? 0) - (b.entry.rank ?? 0)
    if (rank !== 0) return rank
    const width = b.endColumn - b.startColumn - (a.endColumn - a.startColumn)
    if (width !== 0) return width
    if (a.startColumn !== b.startColumn) return a.startColumn - b.startColumn
    return a.entry.start.getTime() - b.entry.start.getTime()
  })

  for (const candidate of candidates) {
    const { startColumn, endColumn } = candidate
    let lane = lanes.findIndex((taken) =>
      taken.every((range) => range.end < startColumn || range.start > endColumn),
    )
    if (lane === -1) {
      lane = lanes.length
      lanes.push([])
    }
    lanes[lane]?.push({ start: startColumn, end: endColumn })

    placed.push({
      item: candidate.entry.item,
      startColumn,
      endColumn,
      lane,
      continuesBefore: candidate.continuesBefore,
      continuesAfter: candidate.continuesAfter,
    })
  }

  return { spans: placed, laneCount: lanes.length }
}

/** Split the 42-day grid into its six weeks. */
export function weeksOf(grid: readonly Date[], daysInWeek = 7): Date[][] {
  const weeks: Date[][] = []
  for (let index = 0; index < grid.length; index += daysInWeek) {
    weeks.push(grid.slice(index, index + daysInWeek))
  }
  return weeks
}
