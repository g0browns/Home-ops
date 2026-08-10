// Units and quantity scaling for recipes (SPEC §4.6).
//
// The vocabulary mirrors backend/src/home_ops/modules/kitchen/units.py — see
// that file for why every volume unit is US customary and why the cup is
// 236.588 ml rather than the 240 ml legal cup. `test_units.py` asserts the two
// lists agree, because a key with no match here would render as a blank unit
// and look like a data-entry mistake rather than a bug. It also asserts the
// factors match, which is what would have caught the Imperial pint that sat
// here until 2026-08-01.
//
// Scaling lives on this side because it is a display concern: dragging the
// servings control rescales what is drawn, and never touches what is stored.
// The recipe as written is the recipe as written.

export type Dimension = 'mass' | 'volume' | 'count'

export interface Unit {
  readonly key: string
  readonly singular: string
  readonly plural: string
  readonly dimension: Dimension
  /** Grams for mass, milliliters for volume, 1 for count. */
  readonly factor: number
}

export const UNITS: readonly Unit[] = [
  { key: 'g', singular: 'gram', plural: 'grams', dimension: 'mass', factor: 1 },
  { key: 'kg', singular: 'kilogram', plural: 'kilograms', dimension: 'mass', factor: 1000 },
  { key: 'oz', singular: 'ounce', plural: 'ounces', dimension: 'mass', factor: 28.349523125 },
  { key: 'lb', singular: 'pound', plural: 'pounds', dimension: 'mass', factor: 453.59237 },
  { key: 'ml', singular: 'milliliter', plural: 'milliliters', dimension: 'volume', factor: 1 },
  { key: 'l', singular: 'liter', plural: 'liters', dimension: 'volume', factor: 1000 },
  // US customary, and each an exact fraction of the next: tsp = tbsp/3,
  // tbsp = floz/2, cup = 8 floz, pt = 2 cups, qt = 2 pt, gal = 4 qt.
  { key: 'tsp', singular: 'teaspoon', plural: 'teaspoons', dimension: 'volume', factor: 4.92892159375 },
  { key: 'tbsp', singular: 'tablespoon', plural: 'tablespoons', dimension: 'volume', factor: 14.78676478125 },
  { key: 'floz', singular: 'fluid ounce', plural: 'fluid ounces', dimension: 'volume', factor: 29.5735295625 },
  { key: 'cup', singular: 'cup', plural: 'cups', dimension: 'volume', factor: 236.5882365 },
  { key: 'pt', singular: 'pint', plural: 'pints', dimension: 'volume', factor: 473.176473 },
  { key: 'qt', singular: 'quart', plural: 'quarts', dimension: 'volume', factor: 946.352946 },
  { key: 'gal', singular: 'gallon', plural: 'gallons', dimension: 'volume', factor: 3785.411784 },
  { key: 'piece', singular: 'piece', plural: 'pieces', dimension: 'count', factor: 1 },
  { key: 'clove', singular: 'clove', plural: 'cloves', dimension: 'count', factor: 1 },
  { key: 'slice', singular: 'slice', plural: 'slices', dimension: 'count', factor: 1 },
  { key: 'pinch', singular: 'pinch', plural: 'pinches', dimension: 'count', factor: 1 },
  { key: 'bunch', singular: 'bunch', plural: 'bunches', dimension: 'count', factor: 1 },
  { key: 'can', singular: 'can', plural: 'cans', dimension: 'count', factor: 1 },
  { key: 'packet', singular: 'packet', plural: 'packets', dimension: 'count', factor: 1 },
]

const BY_KEY = new Map(UNITS.map((unit) => [unit.key, unit]))

export function findUnit(key: string | null | undefined): Unit | undefined {
  return key ? BY_KEY.get(key) : undefined
}

/**
 * How a unit reads in a picker: "pint (473 ml)".
 *
 * The factor is in the label on purpose. A cook choosing "cup" should be able
 * to see what it means rather than discover it later, and it is the cheapest
 * possible answer to "whose pint is this" — a question this project got wrong
 * once already.
 */
export function unitLabel(unit: Unit): string {
  if (unit.dimension === 'count') return unit.singular
  const base = unit.dimension === 'mass' ? 'g' : 'ml'
  if (unit.factor === 1) return unit.singular
  return `${unit.singular} (${trim(unit.factor)} ${base})`
}

function trim(value: number): string {
  return String(Math.round(value * 100) / 100)
}

/** Singular below two, plural otherwise. "1 clove", "1.5 cups". */
export function unitName(key: string | null | undefined, quantity: number | null): string {
  const found = findUnit(key)
  if (!found) return ''
  return quantity !== null && quantity > 0 && quantity <= 1 ? found.singular : found.plural
}

// --- scaling ------------------------------------------------------------------

/** The fractions a kitchen actually uses. Nothing measures 5/16 of a cup. */
const KITCHEN_FRACTIONS: readonly (readonly [number, number])[] = [
  [1, 8],
  [1, 4],
  [1, 3],
  [1, 2],
  [2, 3],
  [3, 4],
]

export function scaleQuantity(
  quantity: number | null,
  fromServings: number,
  toServings: number,
): number | null {
  // "Salt to taste" has no quantity and must survive scaling untouched rather
  // than becoming 0.
  if (quantity === null) return null
  if (fromServings <= 0 || toServings <= 0) return quantity
  return (quantity * toServings) / fromServings
}

/**
 * A quantity as a cook would write it.
 *
 * Doubling half a cup gives 1, not 1.0; tripling it gives 1 1/2, not 1.5. Big
 * numbers stay decimal, because 437 1/2 grams helps nobody.
 */
export function formatQuantity(quantity: number | null): string {
  if (quantity === null) return ''
  if (!Number.isFinite(quantity)) return ''
  if (quantity === 0) return '0'

  const rounded = Math.round(quantity * 1000) / 1000

  // Above this, fractions stop being useful and start being noise: weights and
  // millilitres live here, and nobody weighs out two thirds of a gram.
  if (rounded >= 10) return String(Math.round(rounded))

  const whole = Math.floor(rounded)
  const remainder = rounded - whole

  if (remainder < 0.01) return String(whole)

  for (const [numerator, denominator] of KITCHEN_FRACTIONS) {
    if (Math.abs(remainder - numerator / denominator) < 0.02) {
      const fraction = `${numerator}/${denominator}`
      return whole === 0 ? fraction : `${whole} ${fraction}`
    }
  }

  // Not a kitchen fraction: two decimal places, with trailing zeros dropped.
  return String(Math.round(rounded * 100) / 100)
}

/** Both halves at once, for a row in a scaled ingredient list. */
export function formatAmount(
  quantity: number | null,
  unitKey: string | null,
  fromServings: number,
  toServings: number,
): string {
  const scaled = scaleQuantity(quantity, fromServings, toServings)
  const amount = formatQuantity(scaled)
  const name = unitName(unitKey, scaled)
  return [amount, name].filter(Boolean).join(' ')
}
