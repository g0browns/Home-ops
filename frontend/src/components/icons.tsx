// Interface iconography, drawn.
//
// SPEC §6 rules out emoji as interface icons, so these are real 16px strokes on
// a shared grid. They are decorative in every current use — the label is always
// present in the accessible name — hence aria-hidden throughout.

interface IconProps {
  readonly path: string
}

function Glyph({ path }: IconProps) {
  return (
    <svg
      className="icon"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={path} />
    </svg>
  )
}

const PATHS: Record<string, string> = {
  household: 'M2 7l6-4.5L14 7 M3.5 6.2V13h9V6.2 M6.5 13V9h3v4',
  calendar: 'M2.5 4.5h11v9h-11z M2.5 7.5h11 M5.5 2.5v3 M10.5 2.5v3',
  tasks: 'M2.5 4h2.5l1.5 1.5L9.5 2 M2.5 9h2.5l1.5 1.5L9.5 7.5 M11 4.5h3 M11 10h3',
  notes: 'M3.5 2h6.5l2.5 2.5V14h-9z M9.5 2v3h3 M5.5 8h5 M5.5 10.5h3',
  kitchen: 'M2.5 6.5h11v5a2 2 0 0 1-2 2h-7a2 2 0 0 1-2-2z M5.5 6.5V3 M8 6.5V2.5 M10.5 6.5V3',
  shopping: 'M3 5.5h10l-1 8H4z M5.8 5.5V3.8a2.2 2.2 0 0 1 4.4 0v1.7',
  contacts: 'M8 7.8a2.4 2.4 0 1 0 0-4.8 2.4 2.4 0 0 0 0 4.8z M2.8 13.5c0-2.5 2.3-4 5.2-4s5.2 1.5 5.2 4',
  health: 'M1.8 8.5h2.8L6 5.2l2.2 6.2L9.7 8.5h4.5',
  settings:
    'M8 10a2 2 0 1 0 0-4 2 2 0 0 0 0 4z M8 1.5v1.8 M8 12.7v1.8 M1.5 8h1.8 M12.7 8h1.8 M3.4 3.4l1.3 1.3 M11.3 11.3l1.3 1.3 M12.6 3.4l-1.3 1.3 M4.7 11.3l-1.3 1.3',
  sidebar: 'M2.5 3h11v10h-11z M6.5 3v10',
  sun: 'M8 10.6a2.6 2.6 0 1 0 0-5.2 2.6 2.6 0 0 0 0 5.2z M8 1.5v1.4 M8 13.1v1.4 M1.5 8h1.4 M13.1 8h1.4 M3.6 3.6l1 1 M11.4 11.4l1 1 M12.4 3.6l-1 1 M4.6 11.4l-1 1',
  moon: 'M13 9.6A5.6 5.6 0 0 1 6.4 3 5.6 5.6 0 1 0 13 9.6z',
  monitor: 'M2 3h12v7.5H2z M6 13.5h4 M8 10.5v3',
  check: 'M3 8.5l3.2 3L13 4.5',
  chevron: 'M6 4l4 4-4 4',
  signout: 'M6 3.5H3.5v9H6 M9 5.5L11.5 8 9 10.5 M11.5 8h-6',
  trash: 'M2.5 4.5h11 M6 4.5V3h4v1.5 M4 4.5l.7 9h6.6l.7-9 M6.5 7v4 M9.5 7v4',
  pin: 'M6 2h4l-.5 4 2 2.5H4.5L6.5 6z M8 8.5V14',
  pencil: 'M11 2.5l2.5 2.5-8 8H3v-2.5z M9.5 4l2.5 2.5',
  up: 'M8 12.5V3.5 M4 7.5L8 3.5l4 4',
  down: 'M8 3.5v9 M4 8.5l4 4 4-4',
  grip: 'M6 4h.01 M10 4h.01 M6 8h.01 M10 8h.01 M6 12h.01 M10 12h.01',
}

export type IconName = keyof typeof PATHS

export function Icon({ name }: { readonly name: IconName }) {
  return <Glyph path={PATHS[name] ?? PATHS.household ?? ''} />
}

/** Nav labels map to icons by lower-cased label; unbuilt modules included. */
export function iconForNav(label: string): IconName {
  const key = label.toLowerCase()
  return (key in PATHS ? key : 'household') as IconName
}
