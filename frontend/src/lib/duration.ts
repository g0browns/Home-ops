// Cooking times, written the way a person says them (SPEC §4.6).
//
// Recipes store minutes, because that is the one unit that adds up without
// arithmetic. Showing them back as minutes stops working quickly: a rack of
// ribs is "360 min cooking", which is a number you have to convert in your head
// before it means anything.
//
// Pure and tested, like the other lib modules. The rules are small but they are
// the kind that go subtly wrong — 60 reading as "0 hr 60 min", 90 as "1.5 hr".

/** Under this, minutes are simply clearer. Nobody says "0 hr 45 min". */
const HOUR = 60

export function formatDuration(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined) return ''
  if (!Number.isFinite(minutes) || minutes <= 0) return ''

  const whole = Math.round(minutes)
  if (whole < HOUR) return `${whole} min`

  const hours = Math.floor(whole / HOUR)
  const rest = whole % HOUR

  // "2 hr" rather than "2 hr 0 min": a trailing zero reads as precision that
  // is not there.
  if (rest === 0) return `${hours} hr`
  return `${hours} hr ${rest} min`
}

/** "20 min prep", "6 hr cooking" — empty when there is no time to show. */
export function labelledDuration(minutes: number | null | undefined, label: string): string {
  const formatted = formatDuration(minutes)
  return formatted ? `${formatted} ${label}` : ''
}
