// Reading a shopping list (SPEC §4.12).
//
// Pure, and separate from the page for the usual reason: the grouping rule cost
// a defect to get right and a rule that cost a defect deserves a test that keeps
// it right.

import type { ShoppingItem } from '../api/client'
import { formatQuantity, unitName } from './units'

/** The amount as a cook would read it: "1.5 kg", "2 cups", or nothing at all. */
export function amountOf(item: ShoppingItem): string {
  if (item.quantity === null) return ''
  const quantity = Number(item.quantity)
  if (!Number.isFinite(quantity)) return ''
  return [formatQuantity(quantity), unitName(item.unit, quantity)].filter(Boolean).join(' ')
}

/** "Anything else" rather than a blank heading: the leftovers are a real part
    of the walk, they just have no section of their own. */
export const NO_SECTION = 'Anything else'

/**
 * Lines in the order the shop is walked, grouped under their section.
 *
 * Grouped by key rather than by adjacency. The server orders the list by
 * position, which was assigned by section *at the time the list was generated*
 * — so a manual line added later, or a section named after the fact, arrives
 * out of that order and an adjacency grouping renders "Anything else" twice
 * with a named section wedged between them.
 */
export function bySection(items: readonly ShoppingItem[]): { section: string; items: ShoppingItem[] }[] {
  const groups = new Map<string, ShoppingItem[]>()
  for (const item of items) {
    const section = item.section ?? NO_SECTION
    const existing = groups.get(section)
    if (existing) existing.push(item)
    else groups.set(section, [item])
  }

  return [...groups.entries()]
    .map(([section, group]) => ({ section, items: group }))
    // Named sections alphabetically, the leftovers last — the same rule the
    // server's aggregation sorts by, so the two agree.
    .sort((a, b) => {
      if (a.section === NO_SECTION) return 1
      if (b.section === NO_SECTION) return -1
      return a.section.localeCompare(b.section)
    })
}
