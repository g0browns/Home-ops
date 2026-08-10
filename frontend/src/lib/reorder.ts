// Moving an item within a list.
//
// Pulled out as a pure function because drag-and-drop is the one interaction
// that is genuinely awkward to test through the DOM, and almost all of the ways
// it goes wrong are index arithmetic: dropping an item on itself, dropping past
// the end, or an off-by-one when moving forwards versus backwards.

/**
 * Return a new list with the item at `from` moved to `to`.
 *
 * The subtlety: after removing the item, every index above it shifts down by
 * one. Splicing it back in at the original `to` is therefore correct when
 * moving *backwards* and one place short when moving *forwards*. `splice`
 * handles that for us because we compute `to` against the already-shortened
 * list — which is why the removal happens first.
 *
 * Out-of-range indices return the list unchanged rather than throwing: a drop
 * can legitimately land nowhere, and a reorder is not worth an exception.
 */
export function reorder<T>(items: readonly T[], from: number, to: number): T[] {
  if (from === to) return [...items]
  if (from < 0 || from >= items.length) return [...items]
  if (to < 0 || to >= items.length) return [...items]

  const next = [...items]
  const [moved] = next.splice(from, 1)
  if (moved === undefined) return [...items]

  next.splice(to, 0, moved)
  return next
}

/** Move an item one place towards the front. The keyboard equivalent of a drag. */
export function moveEarlier<T>(items: readonly T[], index: number): T[] {
  return reorder(items, index, index - 1)
}

/** Move an item one place towards the back. */
export function moveLater<T>(items: readonly T[], index: number): T[] {
  return reorder(items, index, index + 1)
}
