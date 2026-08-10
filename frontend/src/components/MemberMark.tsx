// A member's colour, wherever they appear (SPEC §6, "Rota").
//
// Two shapes, one rule: the hue never appears without the name or the initials
// beside it. A member who cannot distinguish clay from ochre still reads "Ali"
// and "Maya" — colour is here to make scanning fast, not to carry meaning.

import { hueVar, initials } from '../lib/members'

export interface MemberLike {
  readonly display_name: string
  readonly avatar_color: string | null
}

/** A filled chip carrying the member's name. Used in dense lists. */
export function MemberChip({ member }: { readonly member: MemberLike }) {
  return (
    <span className="member-chip" style={{ background: hueVar(member.avatar_color) }}>
      {member.display_name}
    </span>
  )
}

/** A square initials mark. Used where a full name will not fit. */
export function MemberMark({
  member,
  size = 'md',
}: {
  readonly member: MemberLike
  readonly size?: 'sm' | 'md'
}) {
  return (
    <span
      className="member-mark"
      data-size={size}
      style={{ background: hueVar(member.avatar_color) }}
      // The initials are decorative here; every use sits next to the real name.
      aria-hidden="true"
    >
      {initials(member.display_name)}
    </span>
  )
}

/**
 * The 3px leading-edge bar that makes a row scannable by colour.
 *
 * Renders the whole `<td>` rather than a span inside one. A child element
 * cannot be made to span a table row's height — `align-self: stretch` needs a
 * flex parent, and a `<td>` is not one — so the bar came out a fixed 20px on a
 * 32px row and the effect only half worked. Colouring the cell itself gets full
 * height for free, at any row height, forever.
 */
export function MemberBarCell({ member }: { readonly member: MemberLike }) {
  return (
    <td
      className="lead"
      style={{ background: hueVar(member.avatar_color) }}
      aria-hidden="true"
    />
  )
}
