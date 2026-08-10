// Where to draw a context menu (SPEC §4.3).
//
// Pure arithmetic, and a lib rather than inline in the component for one reason:
// every interesting case is at the edge of the screen, which is exactly where a
// hand-checked implementation is wrong and nobody notices until they right-click
// the last event in the last week of the month.
//
// The rule is *flip, don't slide*. A menu that slides back inside the viewport
// ends up sitting under the pointer, so the first item is already highlighted and
// a second click lands on it. Flipping it to the other side of the pointer keeps
// the whole menu clear of the thing it was opened on.

export interface Point {
  readonly x: number
  readonly y: number
}

export interface Size {
  readonly width: number
  readonly height: number
}

export interface Placement {
  readonly left: number
  readonly top: number
  /** Which way it opened. The caller uses it for the transform origin, so the
      menu appears to grow out of the pointer rather than towards it. */
  readonly origin: 'top-left' | 'top-right' | 'bottom-left' | 'bottom-right'
}

/** Kept off the viewport edge, so the menu never looks pasted to the border. */
export const MENU_MARGIN = 8

/**
 * Place a menu of `size` at `point`, inside `viewport`.
 *
 * Opens down-and-right by default, which is what a pointer expects. Flips up
 * when there is no room below, and left when there is no room to the right —
 * independently, because a corner needs both.
 */
export function menuPosition(point: Point, size: Size, viewport: Size): Placement {
  const fitsBelow = point.y + size.height + MENU_MARGIN <= viewport.height
  const fitsRight = point.x + size.width + MENU_MARGIN <= viewport.width

  // Flipping needs room on the other side too. A menu taller than the viewport
  // has neither, and then the top edge is the least bad answer: the first item
  // is the one you can still reach.
  const roomAbove = point.y - size.height - MENU_MARGIN >= 0
  const roomLeft = point.x - size.width - MENU_MARGIN >= 0

  const openUp = !fitsBelow && roomAbove
  const openLeft = !fitsRight && roomLeft

  const top = openUp ? point.y - size.height : point.y
  const left = openLeft ? point.x - size.width : point.x

  return {
    // Clamped as a backstop for the case where neither side fits — a menu larger
    // than the window. Never reached by a menu that fits, because the branches
    // above have already kept it inside.
    left: clamp(left, MENU_MARGIN, Math.max(MENU_MARGIN, viewport.width - size.width - MENU_MARGIN)),
    top: clamp(top, MENU_MARGIN, Math.max(MENU_MARGIN, viewport.height - size.height - MENU_MARGIN)),
    origin: `${openUp ? 'bottom' : 'top'}-${openLeft ? 'right' : 'left'}` as Placement['origin'],
  }
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), high)
}

/**
 * The point a `contextmenu` event should open at.
 *
 * **Keyboard invocation is the reason this exists.** Shift+F10 and the Menu key
 * fire a real `contextmenu` event, which is what makes a right-click menu
 * reachable without a mouse for free — but they report no useful coordinates.
 * Chrome sends 0,0; other browsers have sent the element's corner or −1. Trusting
 * `clientX` there puts the menu in the top-left corner of the window, nowhere
 * near the thing it belongs to.
 *
 * So: use the pointer only when the event actually came from one, and otherwise
 * fall back to the bottom-left of the element it was invoked on, which is where a
 * menu belonging to that element ought to appear.
 */
export function contextMenuPoint(
  event: { readonly clientX: number; readonly clientY: number; readonly detail?: number },
  target: { readonly left: number; readonly bottom: number },
): Point {
  // `detail` is 0 for a keyboard-invoked contextmenu and non-zero for a mouse
  // one; the coordinate check catches the browsers that send 0,0 with a detail
  // this code cannot rely on.
  const fromPointer = (event.detail ?? 0) !== 0 || event.clientX > 0 || event.clientY > 0
  return fromPointer
    ? { x: event.clientX, y: event.clientY }
    : { x: target.left, y: target.bottom }
}
