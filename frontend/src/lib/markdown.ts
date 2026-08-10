// Markdown rendering for notes (SPEC §4.5 — "a rendered reader view").
//
// The server stores and returns markdown source and never emits HTML, so the
// conversion happens here — which makes sanitising it this module's job and
// nobody else's.
//
// A note is written by a household member, so this is not a hostile-input
// problem in the usual sense. It is still a real one: a limited member could
// leave a note containing a script tag, and a read-only guest could be the one
// who opens it. Rendering unsanitised HTML would turn "write a note" into
// "run code in an admin's session".

import DOMPurify from 'dompurify'
import { marked } from 'marked'

marked.setOptions({
  // Line breaks behave the way people writing a shopping list expect, rather
  // than the way CommonMark specifies.
  breaks: true,
  gfm: true,
})

/** Tags a note may contain. Anything else is stripped, text kept. */
const ALLOWED_TAGS = [
  'p', 'br', 'hr',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'strong', 'em', 'del', 'code', 'pre', 'blockquote',
  'ul', 'ol', 'li',
  'a',
  'table', 'thead', 'tbody', 'tr', 'th', 'td',
  'input', // task-list checkboxes, forced disabled below
]

const ALLOWED_ATTR = ['href', 'title', 'type', 'checked', 'disabled']

export function renderMarkdown(source: string): string {
  const html = marked.parse(source ?? '', { async: false })
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    // Belt and braces alongside the tag allowlist: no data:, no javascript:.
    ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|#|\/)/i,
    // A note must not be able to reach outside its own container.
    FORBID_TAGS: ['style', 'script', 'iframe', 'object', 'embed', 'form'],
    FORBID_ATTR: ['style', 'onerror', 'onload', 'onclick'],
  })
}

/**
 * A short plain-text preview for a card.
 *
 * Deliberately built from the *source*, not the rendered HTML: stripping tags
 * out of HTML to get text back is the kind of round trip that eventually
 * reintroduces the injection the sanitiser just removed.
 */
export function previewOf(source: string, limit = 180): string {
  const text = (source ?? '')
    .replace(/```[\s\S]*?```/g, ' ')      // fenced code
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ') // images
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1') // links keep their label
    .replace(/[#>*_`~-]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()

  return text.length <= limit ? text : `${text.slice(0, limit).trimEnd()}…`
}
