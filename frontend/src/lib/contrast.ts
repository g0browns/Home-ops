// WCAG relative luminance and contrast ratio.
//
// Exists so the accessibility promise in SPEC §6 is enforced by the build rather
// than by having checked once. `tokens.contrast.test.ts` parses tokens.css and
// runs every text-on-ground pair through this, in both themes.

export interface Rgb {
  readonly r: number
  readonly g: number
  readonly b: number
}

export function parseHex(hex: string): Rgb {
  const value = hex.trim().replace(/^#/, '')
  const full =
    value.length === 3
      ? value
          .split('')
          .map((c) => c + c)
          .join('')
      : value

  if (!/^[0-9a-fA-F]{6}$/.test(full)) {
    throw new Error(`Not a hex colour: ${hex}`)
  }

  return {
    r: parseInt(full.slice(0, 2), 16),
    g: parseInt(full.slice(2, 4), 16),
    b: parseInt(full.slice(4, 6), 16),
  }
}

function channel(value: number): number {
  const sRGB = value / 255
  return sRGB <= 0.04045 ? sRGB / 12.92 : ((sRGB + 0.055) / 1.055) ** 2.4
}

export function relativeLuminance({ r, g, b }: Rgb): number {
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

/** WCAG 2.1 contrast ratio, 1–21. AA wants 4.5 for body text, 3 for large. */
export function contrastRatio(foreground: string, background: string): number {
  const a = relativeLuminance(parseHex(foreground))
  const b = relativeLuminance(parseHex(background))
  const [lighter, darker] = a > b ? [a, b] : [b, a]
  return (lighter + 0.05) / (darker + 0.05)
}
