// Bringing an existing Mealie library across (SPEC §4.6).
//
// Two steps on purpose. Choosing a file shows what *would* happen; a second,
// explicit click does it. Importing eighty recipes into a household library is
// not something to discover you have done — especially the replace case, which
// throws away recipes that are already here.
//
// It lives on the Kitchen page rather than in Settings: importing recipes is a
// kitchen write, and Settings' household section is gated on `settings` write,
// which would hide this from somebody perfectly entitled to use it.

import { useState } from 'react'

import { errorMessage, importMealie, type MealieImportResult } from '../api/client'

type Conflict = 'skip' | 'replace'

export function MealieImport({ onImported }: { readonly onImported: () => void }) {
  const [open, setOpen] = useState(false)
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<MealieImportResult | null>(null)
  const [onConflict, setOnConflict] = useState<Conflict>('skip')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<MealieImportResult | null>(null)

  function reset() {
    setFile(null)
    setPreview(null)
    setError(null)
    setDone(null)
    setOnConflict('skip')
  }

  async function choose(chosen: File | undefined, conflict: Conflict = onConflict) {
    if (!chosen) return
    setFile(chosen)
    setBusy(true)
    setError(null)
    setDone(null)

    const result = await importMealie(chosen, { preview: true, onConflict: conflict })
    setBusy(false)
    if (result.ok) setPreview(result.data)
    else {
      setPreview(null)
      // The server's message names what was wrong with the archive — an unsafe
      // path, a bomb, no recipes in it. "Import failed" would name none of them.
      setError(errorMessage(result.data, 'Could not read that archive.'))
    }
  }

  async function commit() {
    if (!file) return
    setBusy(true)
    setError(null)
    const result = await importMealie(file, { preview: false, onConflict })
    setBusy(false)
    if (result.ok) {
      setDone(result.data)
      setPreview(null)
      setFile(null)
      onImported()
    } else {
      setError(errorMessage(result.data, 'Could not import that archive.'))
    }
  }

  if (!open) {
    return (
      <p className="reorder-hint">
        Moving from Mealie?{' '}
        <button type="button" className="link-button" onClick={() => setOpen(true)}>
          Import a Mealie export
        </button>
      </p>
    )
  }

  return (
    <section className="quick-add mealie-import" aria-label="Import from Mealie">
      <div className="field" style={{ flex: '1 1 100%' }}>
        <label htmlFor="mealie-file">Mealie ZIP export</label>
        <input
          id="mealie-file"
          type="file"
          accept=".zip,application/zip"
          onChange={(event) => void choose(event.currentTarget.files?.[0])}
        />
        <span className="field-hint">
          Export from Mealie with recipes included and upload the .zip. Nothing is
          imported until you say so.
        </span>
      </div>

      {busy && <p className="loading">Reading the archive…</p>}

      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}

      {preview && (
        <div className="mealie-preview">
          <p className="notice">
            Found <strong>{preview.found}</strong>{' '}
            {preview.found === 1 ? 'recipe' : 'recipes'}
            {/* found_with_images, not with_images: this sentence describes the
                archive, and with_images counts what would actually be stored —
                zero when everything is being skipped. */}
            {preview.found_with_images > 0 && <>, {preview.found_with_images} with pictures</>}.
            {preview.skipped_unreadable > 0 && (
              <>
                {' '}
                <strong>{preview.skipped_unreadable}</strong>{' '}
                {preview.skipped_unreadable === 1 ? 'file was' : 'files were'} not
                recipes we could read.
              </>
            )}
          </p>

          {preview.conflict_count > 0 && (
            <>
              <p className="notice">
                {/* The count comes from conflict_count, not from the length of
                    the list beside it: that list is truncated for display. */}
                <strong>{preview.conflict_count}</strong> already exist here by name:{' '}
                {preview.conflicts.slice(0, 6).join(', ')}
                {preview.conflict_count > 6 && ' and others'}.
              </p>
              <fieldset className="field scope-picker">
                <legend>Those that already exist</legend>
                <div className="scope-options">
                  {(
                    [
                      ['skip', 'Keep mine', 'Leave the recipe that is already here alone'],
                      ['replace', 'Use the imported one', 'Throws away the recipe that is here'],
                    ] as const
                  ).map(([value, label, hint]) => (
                    <label
                      key={value}
                      className="scope-option"
                      data-chosen={onConflict === value}
                    >
                      <input
                        type="radio"
                        name="on-conflict"
                        value={value}
                        checked={onConflict === value}
                        onChange={() => {
                          setOnConflict(value)
                          // Re-preview, so the numbers on screen always match
                          // the choice showing beside them.
                          if (file) void choose(file, value)
                        }}
                      />
                      <span>
                        <strong>{label}</strong>
                        <small>{hint}</small>
                      </span>
                    </label>
                  ))}
                </div>
              </fieldset>
            </>
          )}

          <div className="editor-actions">
            <button type="button" className="button" onClick={() => void commit()} disabled={busy}>
              {onConflict === 'replace' && preview.replaced > 0
                ? `Import ${preview.imported + preview.replaced}, replacing ${preview.replaced}`
                : `Import ${preview.imported} ${preview.imported === 1 ? 'recipe' : 'recipes'}`}
            </button>
            <button type="button" onClick={reset}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {done && (
        <p className="notice">
          Imported <strong>{done.imported}</strong>
          {done.replaced > 0 && <>, replaced {done.replaced}</>}
          {done.skipped_existing > 0 && <>, skipped {done.skipped_existing} already here</>}.
          <button type="button" className="link-button" onClick={() => setOpen(false)}>
            Done
          </button>
        </p>
      )}
    </section>
  )
}
