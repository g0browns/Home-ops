// A right-click menu (SPEC §4.3).
//
// **Not built on `<dialog>`**, unlike everything else in this app that floats.
// `Modal` uses it deliberately — for the focus trap, the inert background and the
// backdrop — and a context menu must have none of those. It is a transient
// pointer affordance: the page behind it stays live, and moving focus away is how
// you dismiss it rather than something to prevent.
//
// So the focus handling is hand-written here, which is the thing `Modal`'s
// docstring warns is easy to get subtly wrong. The three that matter:
//
//   * The first item is focused on open, or a keyboard user has a menu they
//     cannot reach.
//   * Escape closes it AND returns focus to whatever opened it. Closing without
//     restoring focus drops the caret at the top of the document, so the next
//     Tab starts from the beginning of the page.
//   * Anything that moves the page under it — a scroll, a resize — closes it,
//     because it is positioned in viewport coordinates and would otherwise sit
//     over the wrong event.
//
// Rendered through `createPortal` into `document.body` for the same reason
// `.drag-ghost` is: a `position: fixed` element is re-anchored by any ancestor
// with a transform, a filter or containment, and a portal removes the question
// rather than betting on the ancestors never acquiring one.

import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import { createPortal } from 'react-dom'

import { menuPosition, type Placement, type Point } from '../lib/menuPosition'

export interface MenuItem {
  readonly label: string
  readonly onSelect: () => void
  /** Draws it as the destructive one. A word as well as a colour — colour is
      never the only signal in this app. */
  readonly danger?: boolean
}

export function ContextMenu({
  at,
  items,
  label,
  onClose,
}: {
  readonly at: Point
  readonly items: readonly MenuItem[]
  /** Names the menu for a screen reader: "Swimming lesson", not "menu". */
  readonly label: string
  readonly onClose: () => void
}) {
  const menu = useRef<HTMLDivElement>(null)
  const [placement, setPlacement] = useState<Placement | null>(null)

  // Measured, then placed, in one commit before the browser paints. The menu has
  // to exist to know how big it is, and `useLayoutEffect` is what stops it being
  // visible at the wrong position for a frame first.
  useLayoutEffect(() => {
    const node = menu.current
    if (!node) return
    const box = node.getBoundingClientRect()
    setPlacement(
      menuPosition(at, { width: box.width, height: box.height }, {
        width: window.innerWidth,
        height: window.innerHeight,
      }),
    )
  }, [at])

  // Whatever had focus when this opened — the bar that was right-clicked.
  // Captured in a ref during the first render rather than passed in, so the
  // caller cannot forget to, and so it survives the re-render below.
  const opener = useRef<HTMLElement | null>(null)
  if (opener.current === null) opener.current = document.activeElement as HTMLElement | null

  // Focused only once the menu is actually **visible**, which is why this is
  // keyed on `placement` and not done on mount.
  //
  // The first draft focused in the mount effect and the item never took focus:
  // between mounting and being measured the menu is `visibility: hidden`, and a
  // hidden element cannot be focused — `focus()` returns quietly and does
  // nothing. Measured by asking the page what `document.activeElement` was, which
  // is the only way this sort of thing gets noticed.
  useEffect(() => {
    if (!placement) return
    menu.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus()
  }, [placement])

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.stopPropagation()
        opener.current?.focus()
        onClose()
        return
      }
      // Tab closes rather than moving through the items: a menu is a detour, and
      // tabbing out of it should put you back in the page, not part-way through a
      // list you have left behind.
      if (event.key === 'Tab') {
        onClose()
        return
      }
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return

      event.preventDefault()
      const all = [...(menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])]
      if (all.length === 0) return
      const here = all.indexOf(document.activeElement as HTMLElement)
      const step = event.key === 'ArrowDown' ? 1 : -1
      // Wraps, which is what a menu of two or three items wants: the end of a
      // short list is never far from the start.
      all[(here + step + all.length) % all.length]?.focus()
    }

    // `pointerdown`, not `click`: dismissing on the press means the click that
    // dismissed it does not also activate whatever was underneath.
    function onPointerDown(event: PointerEvent) {
      if (!menu.current?.contains(event.target as Node)) onClose()
    }

    // Capture phase, because the calendar scrolls inside its own containers and a
    // scroll there does not bubble to the window.
    const closeOnMove = () => onClose()

    document.addEventListener('keydown', onKeyDown, true)
    document.addEventListener('pointerdown', onPointerDown, true)
    document.addEventListener('scroll', closeOnMove, true)
    window.addEventListener('resize', closeOnMove)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      document.removeEventListener('pointerdown', onPointerDown, true)
      document.removeEventListener('scroll', closeOnMove, true)
      window.removeEventListener('resize', closeOnMove)
    }
  }, [onClose])

  return createPortal(
    <div
      ref={menu}
      className="context-menu"
      role="menu"
      aria-label={label}
      data-origin={placement?.origin}
      style={
        {
          left: placement?.left ?? at.x,
          top: placement?.top ?? at.y,
          // Hidden for the one frame between mounting and being measured.
          // Without this the menu is briefly drawn at the raw pointer position,
          // which at the edge of the screen means visibly jumping into place.
          visibility: placement ? 'visible' : 'hidden',
        } as CSSProperties
      }
    >
      {items.map((item) => (
        <button
          key={item.label}
          type="button"
          role="menuitem"
          className="context-menu-item"
          data-danger={item.danger ? '' : undefined}
          onClick={() => {
            // Closed first: every action here opens something else, and a menu
            // still on screen behind a modal is a menu that outlives its context.
            onClose()
            item.onSelect()
          }}
        >
          {item.label}
        </button>
      ))}
    </div>,
    document.body,
  )
}
