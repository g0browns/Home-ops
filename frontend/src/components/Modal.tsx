// A modal dialog, once, for everything that needs one.
//
// Built on the native `<dialog>` rather than a div with a high z-index. That
// gets focus trapping, Escape, returning focus to whatever opened it, and an
// inert background from the browser — all easy to hand-roll badly and tedious
// to hand-roll well. It is also not a secure-context API, which matters: SPEC
// §2.1 has two of the three access paths on plain HTTP.
//
// Extracted from `RecipePreview`, which had the only copy, when the calendar
// and the contact editor needed the same thing. Three copies of focus handling
// is three chances to get it subtly different.

import { useEffect, useRef, type ReactNode } from 'react'

export function Modal({
  title,
  onClose,
  children,
  footer,
  wide = false,
  labelledBy,
}: {
  /** Shown as the dialog's heading, and used as its accessible name. */
  readonly title: ReactNode
  readonly onClose: () => void
  readonly children: ReactNode
  readonly footer?: ReactNode
  /** For content that needs the room — a day's events, a long form. */
  readonly wide?: boolean
  readonly labelledBy?: string
}) {
  const dialog = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    // `showModal` rather than `show`: the modal behaviour — focus trap, inert
    // background, Escape — only comes with the former.
    const node = dialog.current
    if (node && !node.open) node.showModal()
  }, [])

  return (
    <dialog
      ref={dialog}
      className="modal"
      data-wide={wide}
      aria-labelledby={labelledBy ?? 'modal-title'}
      // Fires for the close button, for Escape, and for the backdrop click
      // below — so there is one way out and one callback for it.
      onClose={onClose}
      onClick={(event) => {
        // A click on the backdrop lands on the dialog element itself, because
        // the backdrop is a pseudo-element with no separate target. Anything
        // inside stops here.
        if (event.target === dialog.current) dialog.current?.close()
      }}
    >
      <div className="modal-head">
        <h2 id={labelledBy ?? 'modal-title'}>{title}</h2>
        <button
          type="button"
          className="icon-button"
          onClick={() => dialog.current?.close()}
          aria-label="Close"
        >
          ×
        </button>
      </div>

      <div className="modal-body">{children}</div>

      {footer && <div className="modal-foot">{footer}</div>}
    </dialog>
  )
}
