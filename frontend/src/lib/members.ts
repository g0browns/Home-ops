// Member colour, the load-bearing idea in the Rota direction (SPEC §6).
//
// Each household member owns a hue that follows them into every list they
// appear in, so you find your own rows by colour before reading a word.
//
// Two rules keep that from becoming an accessibility failure:
//
//   1. The stored value is a KEY (`clay`, `forest`, …), never a hex. The hex
//      lives in tokens.css and differs between themes, so storing one would
//      pin a member to a colour that is unreadable in the other theme.
//   2. A hue is never the only signal. Every place a hue appears, a name or
//      initials appear with it. Colour buys speed here, not meaning.

export const MEMBER_HUES = ['clay', 'forest', 'ochre', 'indigo', 'plum', 'teal'] as const

export type MemberHue = (typeof MEMBER_HUES)[number]

export function isMemberHue(value: unknown): value is MemberHue {
  return typeof value === 'string' && (MEMBER_HUES as readonly string[]).includes(value)
}

/**
 * The CSS custom property holding this member's hue in the current theme.
 *
 * Falls back to the muted text colour for a member with no hue assigned, which
 * is legible in both themes and visibly "unset" rather than accidentally
 * claiming another member's colour.
 */
export function hueVar(key: string | null | undefined): string {
  return isMemberHue(key) ? `var(--member-${key})` : 'var(--text-secondary)'
}

/**
 * Initials for an avatar mark. Up to two, from the first and last word.
 *
 * Uses `Array.from` rather than indexing so a name starting with an emoji or an
 * astral-plane character does not get sliced in half into a replacement glyph.
 */
export function initials(displayName: string): string {
  const words = displayName.trim().split(/\s+/).filter(Boolean)
  if (words.length === 0) return '?'

  const first = Array.from(words[0] ?? '')
  if (words.length === 1) {
    return first.slice(0, 2).join('').toUpperCase()
  }

  const last = Array.from(words[words.length - 1] ?? '')
  return `${first[0] ?? ''}${last[0] ?? ''}`.toUpperCase()
}

/** Inline style applying a member's hue, for use as a bar, chip or border. */
export function hueStyle(key: string | null | undefined): { '--member-hue': string } {
  return { '--member-hue': hueVar(key) }
}
