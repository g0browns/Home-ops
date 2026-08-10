// Health records (SPEC §4.8, phase 8).
//
// **Statistics, never interpretations.** §4.8 is explicit and this page is
// written to be dull on purpose: it shows counts, ranges, means and plain
// differences, and nothing that could be read as advice. There is no colour
// that means "bad", no arrow that means "worse", no threshold anywhere. A lab
// reference range is displayed as the two numbers somebody transcribed off
// their own report, beside the value, with no comparison drawn between them.
// That comparison belongs to the member and their doctor.
//
// **Whose records these are is always on screen.** The subject picker is the
// first control, it lists only people whose records you may actually see, and
// it says plainly when a list is empty because nobody has shared rather than
// because there is nothing there.

import { useCallback, useEffect, useState } from 'react'

import {
  addActivity,
  addLabReport,
  addMedication,
  addVital,
  deleteActivity,
  deleteLabReport,
  deleteMedication,
  deleteVital,
  errorMessage,
  getVitalSummary,
  healthExportUrl,
  listActivity,
  listHealthShares,
  listHealthSubjects,
  listLabReports,
  listMedications,
  listUsers,
  listVitals,
  logDose,
  setHealthShares,
  updateMedication,
  type ActivityEntry,
  type CurrentUser,
  type HouseholdMember,
  type LabReport,
  type Medication,
  type VitalKind,
  type VitalReading,
  type VitalSummary,
} from '../api/client'
import { MemberMark } from '../components/MemberMark'

type Tab = 'vitals' | 'medications' | 'labs' | 'activity' | 'sharing'

const TABS: readonly (readonly [Tab, string])[] = [
  ['vitals', 'Vitals'],
  ['medications', 'Medications'],
  ['labs', 'Lab results'],
  ['activity', 'Activity'],
  ['sharing', 'Sharing'],
]

/** How each kind reads, and what it is usually measured in here. US units. */
const VITALS: readonly { key: VitalKind; label: string; unit: string; pair?: boolean }[] = [
  { key: 'weight', label: 'Weight', unit: 'lb' },
  { key: 'blood_pressure', label: 'Blood pressure', unit: 'mmHg', pair: true },
  { key: 'heart_rate', label: 'Heart rate', unit: 'bpm' },
  { key: 'blood_glucose', label: 'Blood glucose', unit: 'mg/dL' },
  { key: 'temperature', label: 'Temperature', unit: '°F' },
  { key: 'oxygen_saturation', label: 'Oxygen saturation', unit: '%' },
  { key: 'respiratory_rate', label: 'Respiratory rate', unit: '/min' },
  { key: 'custom', label: 'Something else', unit: '' },
]

const ACTIVITIES = ['walk', 'run', 'cycle', 'swim', 'gym', 'sport', 'other'] as const
const FORMS = [
  'tablet', 'capsule', 'liquid', 'injection', 'inhaler', 'patch', 'drops', 'cream', 'other',
] as const

function when(value: string): string {
  return new Date(value).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  })
}

function day(value: string): string {
  return new Date(`${value}T00:00:00`).toLocaleDateString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
  })
}

/** Trailing zeros off a decimal string, without turning it into a float. */
function tidy(value: string | null): string {
  if (value === null) return '—'
  return value.includes('.') ? value.replace(/0+$/, '').replace(/\.$/, '') : value
}

export function HealthPage({ me }: { readonly me: CurrentUser }) {
  const [members, setMembers] = useState<HouseholdMember[]>([])
  const [subjectIds, setSubjectIds] = useState<string[]>([me.id])
  const [subjectId, setSubjectId] = useState(me.id)
  const [tab, setTab] = useState<Tab>('vitals')

  const canWrite = me.permissions['health'] === 'write'
  // Whether you may *write* for this subject. Reading and writing are separate
  // questions here: a share lets you read, never write.
  const isSelf = subjectId === me.id

  useEffect(() => {
    void listUsers().then((result) => {
      if (result.ok) setMembers(result.data)
    })
    void listHealthSubjects().then((result) => {
      if (result.ok && result.data.length) setSubjectIds(result.data)
    })
  }, [])

  const memberById = new Map(members.map((member) => [member.id, member]))
  const subject = memberById.get(subjectId)

  return (
    <div className="page">
      <div className="page-head">
        <h1>Health</h1>
        <p className="page-summary">
          {subjectIds.length === 1
            ? 'Your records'
            : `${subjectIds.length} people's records`}
        </p>
      </div>

      <div className="toolbar">
        <label className="visually-hidden" htmlFor="health-subject">
          Whose records
        </label>
        <select
          id="health-subject"
          value={subjectId}
          onChange={(event) => setSubjectId(event.currentTarget.value)}
        >
          {subjectIds.map((id) => (
            <option key={id} value={id}>
              {id === me.id ? 'You' : (memberById.get(id)?.display_name ?? 'Someone')}
            </option>
          ))}
        </select>

        {subject && subjectId !== me.id && <MemberMark member={subject} size="sm" />}

        <div className="segmented" role="group" aria-label="Health section">
          {TABS.map(([key, label]) => (
            <button
              key={key}
              type="button"
              aria-pressed={tab === key}
              onClick={() => setTab(key)}
            >
              {label}
            </button>
          ))}
        </div>

        <a className="link-button" href={healthExportUrl(subjectId)} download="health.csv">
          Export CSV
        </a>
      </div>

      {!isSelf && (
        <p className="notice" role="status">
          You are looking at {subject?.display_name ?? 'somebody'}&rsquo;s records because they
          shared them with you. You can read them; only they can change them.
        </p>
      )}

      {tab === 'vitals' && (
        <Vitals subjectId={subjectId} canWrite={canWrite && isSelf} me={me} />
      )}
      {tab === 'medications' && (
        <Medications subjectId={subjectId} canWrite={canWrite && isSelf} />
      )}
      {tab === 'labs' && <Labs subjectId={subjectId} canWrite={canWrite && isSelf} />}
      {tab === 'activity' && (
        <Activity subjectId={subjectId} canWrite={canWrite && isSelf} />
      )}
      {tab === 'sharing' && (
        <Sharing me={me} members={members} canWrite={canWrite} />
      )}
    </div>
  )
}

// --- vitals -------------------------------------------------------------------

function Vitals({
  subjectId,
  canWrite,
  me,
}: {
  readonly subjectId: string
  readonly canWrite: boolean
  readonly me: CurrentUser
}) {
  const [kind, setKind] = useState<VitalKind>('weight')
  const [readings, setReadings] = useState<VitalReading[]>([])
  const [summary, setSummary] = useState<VitalSummary | null>(null)
  const [error, setError] = useState<string | null>(null)

  const chosen = VITALS.find((entry) => entry.key === kind)!

  const refresh = useCallback(async () => {
    const [rows, stats] = await Promise.all([
      listVitals({ subject_id: subjectId, kind }),
      getVitalSummary(subjectId, kind),
    ])
    if (rows.ok) setReadings(rows.data)
    if (stats.ok) setSummary(stats.data)
  }, [subjectId, kind])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return (
    <>
      <div className="toolbar">
        <label className="visually-hidden" htmlFor="vital-kind">
          Which measurement
        </label>
        <select
          id="vital-kind"
          value={kind}
          onChange={(event) => setKind(event.currentTarget.value as VitalKind)}
        >
          {VITALS.map((entry) => (
            <option key={entry.key} value={entry.key}>
              {entry.label}
            </option>
          ))}
        </select>
      </div>

      {summary && summary.count > 0 && (
        /* Counts, a range, a mean and a plain difference. Nothing here is a
           judgment, and §4.8 forbids one being added. */
        <dl className="stat-row">
          <div>
            <dt>Latest</dt>
            <dd className="tabular">
              {tidy(summary.latest)} {summary.unit}
            </dd>
          </div>
          <div>
            <dt>Lowest</dt>
            <dd className="tabular">{tidy(summary.minimum)}</dd>
          </div>
          <div>
            <dt>Highest</dt>
            <dd className="tabular">{tidy(summary.maximum)}</dd>
          </div>
          <div>
            <dt>Average</dt>
            <dd className="tabular">{tidy(summary.mean)}</dd>
          </div>
          <div>
            <dt>
              Change over {summary.count} {summary.count === 1 ? 'reading' : 'readings'}
            </dt>
            {/* A number, with no colour and no arrow: whether a change is good
                is not ours to say. */}
            <dd className="tabular">{tidy(summary.change)}</dd>
          </div>
        </dl>
      )}

      {canWrite && (
        <AddVital
          kind={kind}
          unit={chosen.unit}
          pair={chosen.pair ?? false}
          subjectId={subjectId}
          onAdded={refresh}
          onError={setError}
        />
      )}
      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}

      {readings.length === 0 ? (
        <p className="empty">No {chosen.label.toLowerCase()} recorded.</p>
      ) : (
        <ul className="record-list">
          {readings.map((reading) => (
            <li key={reading.id} className="record-row">
              <span className="record-value tabular">
                {tidy(reading.value)}
                {reading.secondary_value !== null && `/${tidy(reading.secondary_value)}`}{' '}
                <span className="muted">{reading.unit}</span>
              </span>
              <span className="record-when">{when(reading.measured_at)}</span>
              {reading.note && <span className="muted">{reading.note}</span>}
              {reading.recorded_by_id && reading.recorded_by_id !== reading.subject_id && (
                <span className="muted">recorded by someone else</span>
              )}
              {canWrite && (
                <button
                  type="button"
                  className="icon-button"
                  onClick={async () => {
                    await deleteVital(reading.id)
                    await refresh()
                  }}
                  aria-label={`Delete the reading from ${when(reading.measured_at)}`}
                >
                  ×
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      <p className="field-hint">
        Readings are shown as recorded, for {me.display_name === '' ? 'you' : 'this person'} only.
      </p>
    </>
  )
}

function AddVital({
  kind,
  unit,
  pair,
  subjectId,
  onAdded,
  onError,
}: {
  readonly kind: VitalKind
  readonly unit: string
  readonly pair: boolean
  readonly subjectId: string
  readonly onAdded: () => Promise<void>
  readonly onError: (message: string | null) => void
}) {
  const [value, setValue] = useState('')
  const [second, setSecond] = useState('')
  const [entryUnit, setEntryUnit] = useState(unit)
  const [label, setLabel] = useState('')

  useEffect(() => setEntryUnit(unit), [unit])

  return (
    <form
      className="shopping-add"
      onSubmit={async (event) => {
        event.preventDefault()
        if (!value.trim()) return
        const result = await addVital({
          subject_id: subjectId,
          kind,
          label: kind === 'custom' ? label.trim() || null : null,
          value: value.trim(),
          secondary_value: pair && second.trim() ? second.trim() : null,
          unit: entryUnit.trim() || 'n/a',
          measured_at: new Date().toISOString(),
        })
        if (!result.ok) {
          onError(errorMessage(result.data, 'Could not save that reading.'))
          return
        }
        onError(null)
        setValue('')
        setSecond('')
        await onAdded()
      }}
    >
      {kind === 'custom' && (
        <>
          <label className="visually-hidden" htmlFor="vital-label">
            What are you measuring
          </label>
          <input
            id="vital-label"
            value={label}
            onChange={(event) => setLabel(event.currentTarget.value)}
            placeholder="Peak flow"
          />
        </>
      )}

      <label className="visually-hidden" htmlFor="vital-value">
        Reading
      </label>
      <input
        id="vital-value"
        className="quantity"
        value={value}
        onChange={(event) => setValue(event.currentTarget.value)}
        placeholder={pair ? 'Systolic' : 'Reading'}
        inputMode="decimal"
        required
      />

      {pair && (
        <>
          <label className="visually-hidden" htmlFor="vital-second">
            Diastolic
          </label>
          <input
            id="vital-second"
            className="quantity"
            value={second}
            onChange={(event) => setSecond(event.currentTarget.value)}
            placeholder="Diastolic"
            inputMode="decimal"
          />
        </>
      )}

      <label className="visually-hidden" htmlFor="vital-unit">
        Unit
      </label>
      <input
        id="vital-unit"
        className="quantity"
        value={entryUnit}
        onChange={(event) => setEntryUnit(event.currentTarget.value)}
        placeholder="Unit"
      />

      <button type="submit" className="button">
        Record
      </button>
    </form>
  )
}

// --- medications --------------------------------------------------------------

function Medications({
  subjectId,
  canWrite,
}: {
  readonly subjectId: string
  readonly canWrite: boolean
}) {
  const [rows, setRows] = useState<Medication[]>([])
  const [includeStopped, setIncludeStopped] = useState(false)
  const [name, setName] = useState('')
  const [dose, setDose] = useState('')
  const [form, setForm] = useState('')
  const [stock, setStock] = useState('')
  const [refill, setRefill] = useState('')

  const refresh = useCallback(async () => {
    const result = await listMedications({
      subject_id: subjectId,
      include_stopped: includeStopped,
    })
    if (result.ok) setRows(result.data)
  }, [subjectId, includeStopped])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return (
    <>
      <div className="toolbar">
        <label className="check">
          <input
            type="checkbox"
            checked={includeStopped}
            onChange={(event) => setIncludeStopped(event.currentTarget.checked)}
          />
          Include stopped
        </label>
      </div>

      {canWrite && (
        <form
          className="shopping-add"
          onSubmit={async (event) => {
            event.preventDefault()
            if (!name.trim()) return
            await addMedication({
              subject_id: subjectId,
              name: name.trim(),
              dose: dose.trim() || null,
              form: form || null,
              stock_count: stock.trim() || null,
              refill_at: refill.trim() || null,
            })
            setName('')
            setDose('')
            setStock('')
            setRefill('')
            await refresh()
          }}
        >
          <label className="visually-hidden" htmlFor="med-name">
            Medication
          </label>
          <input
            id="med-name"
            value={name}
            onChange={(event) => setName(event.currentTarget.value)}
            placeholder="Medication"
            required
          />
          <label className="visually-hidden" htmlFor="med-dose">
            Dose
          </label>
          <input
            id="med-dose"
            className="quantity"
            value={dose}
            onChange={(event) => setDose(event.currentTarget.value)}
            placeholder="500 mg"
          />
          <label className="visually-hidden" htmlFor="med-form">
            Form
          </label>
          <select id="med-form" value={form} onChange={(e) => setForm(e.currentTarget.value)}>
            <option value="">form…</option>
            {FORMS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
          <label className="visually-hidden" htmlFor="med-stock">
            How many you have
          </label>
          <input
            id="med-stock"
            className="quantity"
            value={stock}
            onChange={(event) => setStock(event.currentTarget.value)}
            placeholder="Have"
            inputMode="decimal"
          />
          <label className="visually-hidden" htmlFor="med-refill">
            Tell me at
          </label>
          <input
            id="med-refill"
            className="quantity"
            value={refill}
            onChange={(event) => setRefill(event.currentTarget.value)}
            placeholder="Warn at"
            inputMode="decimal"
          />
          <button type="submit" className="button">
            Add
          </button>
        </form>
      )}

      {rows.length === 0 ? (
        <p className="empty">Nothing recorded.</p>
      ) : (
        <ul className="record-list">
          {rows.map((medication) => (
            <li key={medication.id} className="record-row" data-stopped={!medication.is_active}>
              <span className="record-value">{medication.name}</span>
              <span className="muted">
                {[medication.dose, medication.form].filter(Boolean).join(' · ')}
              </span>
              {medication.stock_count !== null && (
                <span className="tabular muted">{tidy(medication.stock_count)} left</span>
              )}
              {medication.needs_refill && (
                /* The member's own two numbers compared — "you said tell me at
                   2, you have 2". Not a statement about their health. */
                <span className="uncombined">time to reorder</span>
              )}
              {!medication.is_active && <span className="muted">stopped</span>}

              {canWrite && medication.is_active && (
                <>
                  <button
                    type="button"
                    onClick={async () => {
                      await logDose(medication.id)
                      await refresh()
                    }}
                  >
                    Took one
                  </button>
                  <button
                    type="button"
                    onClick={async () => {
                      await updateMedication(medication.id, { is_active: false })
                      await refresh()
                    }}
                  >
                    Stop
                  </button>
                </>
              )}
              {canWrite && (
                <button
                  type="button"
                  className="icon-button"
                  onClick={async () => {
                    await deleteMedication(medication.id)
                    await refresh()
                  }}
                  aria-label={`Delete ${medication.name}`}
                >
                  ×
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

// --- lab results --------------------------------------------------------------

function Labs({ subjectId, canWrite }: { readonly subjectId: string; readonly canWrite: boolean }) {
  const [reports, setReports] = useState<LabReport[]>([])
  const [title, setTitle] = useState('')
  const [labName, setLabName] = useState('')
  const [collectedOn, setCollectedOn] = useState('')

  const refresh = useCallback(async () => {
    const result = await listLabReports({ subject_id: subjectId })
    if (result.ok) setReports(result.data)
  }, [subjectId])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return (
    <>
      {canWrite && (
        <form
          className="shopping-add"
          onSubmit={async (event) => {
            event.preventDefault()
            if (!title.trim() || !collectedOn) return
            await addLabReport({
              subject_id: subjectId,
              title: title.trim(),
              lab_name: labName.trim() || null,
              collected_on: collectedOn,
              analytes: [],
            })
            setTitle('')
            setLabName('')
            setCollectedOn('')
            await refresh()
          }}
        >
          <label className="visually-hidden" htmlFor="lab-title">
            What the report was
          </label>
          <input
            id="lab-title"
            value={title}
            onChange={(event) => setTitle(event.currentTarget.value)}
            placeholder="Annual panel"
            required
          />
          <label className="visually-hidden" htmlFor="lab-name">
            Who ran it
          </label>
          <input
            id="lab-name"
            value={labName}
            onChange={(event) => setLabName(event.currentTarget.value)}
            placeholder="Who ran it"
          />
          <label className="visually-hidden" htmlFor="lab-date">
            Date collected
          </label>
          <input
            id="lab-date"
            type="date"
            value={collectedOn}
            onChange={(event) => setCollectedOn(event.currentTarget.value)}
            required
          />
          <button type="submit" className="button">
            Add report
          </button>
        </form>
      )}

      {reports.length === 0 ? (
        <p className="empty">No lab reports recorded.</p>
      ) : (
        reports.map((report) => (
          <section key={report.id} className="lab-report">
            <h2>
              {report.title}
              <span className="muted"> · {day(report.collected_on)}</span>
              {report.lab_name && <span className="muted"> · {report.lab_name}</span>}
            </h2>

            {report.analytes.length > 0 && (
              <div className="table-frame">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Result</th>
                      <th>Value</th>
                      <th>Unit</th>
                      {/* The lab's printed range, transcribed. Shown beside the
                          value with no comparison drawn — §4.8. */}
                      <th>Range on the report</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.analytes.map((analyte) => (
                      <tr key={analyte.id}>
                        <td>{analyte.name}</td>
                        <td className="tabular">
                          {analyte.value !== null ? tidy(analyte.value) : (analyte.text_value ?? '—')}
                        </td>
                        <td className="muted">{analyte.unit ?? ''}</td>
                        <td className="tabular muted">
                          {analyte.reference_text ??
                            (analyte.reference_low !== null || analyte.reference_high !== null
                              ? `${tidy(analyte.reference_low)} – ${tidy(analyte.reference_high)}`
                              : '—')}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {canWrite && (
              <button
                type="button"
                className="link-button"
                onClick={async () => {
                  await deleteLabReport(report.id)
                  await refresh()
                }}
              >
                Delete this report
              </button>
            )}
          </section>
        ))
      )}
    </>
  )
}

// --- activity -----------------------------------------------------------------

function Activity({
  subjectId,
  canWrite,
}: {
  readonly subjectId: string
  readonly canWrite: boolean
}) {
  const [rows, setRows] = useState<ActivityEntry[]>([])
  const [kind, setKind] = useState<string>('walk')
  const [minutes, setMinutes] = useState('')
  const [miles, setMiles] = useState('')

  const refresh = useCallback(async () => {
    const result = await listActivity({ subject_id: subjectId })
    if (result.ok) setRows(result.data)
  }, [subjectId])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return (
    <>
      {canWrite && (
        <form
          className="shopping-add"
          onSubmit={async (event) => {
            event.preventDefault()
            await addActivity({
              subject_id: subjectId,
              kind,
              happened_at: new Date().toISOString(),
              duration_minutes: minutes.trim() ? Number(minutes) : null,
              distance_miles: miles.trim() || null,
            })
            setMinutes('')
            setMiles('')
            await refresh()
          }}
        >
          <label className="visually-hidden" htmlFor="activity-kind">
            What you did
          </label>
          <select
            id="activity-kind"
            value={kind}
            onChange={(event) => setKind(event.currentTarget.value)}
          >
            {ACTIVITIES.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
          <label className="visually-hidden" htmlFor="activity-minutes">
            Minutes
          </label>
          <input
            id="activity-minutes"
            className="quantity"
            value={minutes}
            onChange={(event) => setMinutes(event.currentTarget.value)}
            placeholder="Minutes"
            inputMode="numeric"
          />
          <label className="visually-hidden" htmlFor="activity-miles">
            Miles
          </label>
          <input
            id="activity-miles"
            className="quantity"
            value={miles}
            onChange={(event) => setMiles(event.currentTarget.value)}
            placeholder="Miles"
            inputMode="decimal"
          />
          <button type="submit" className="button">
            Record
          </button>
        </form>
      )}

      {rows.length === 0 ? (
        <p className="empty">Nothing recorded.</p>
      ) : (
        <ul className="record-list">
          {rows.map((entry) => (
            <li key={entry.id} className="record-row">
              <span className="record-value">{entry.label ?? entry.kind}</span>
              {entry.duration_minutes !== null && (
                <span className="tabular muted">{entry.duration_minutes} min</span>
              )}
              {entry.distance_miles !== null && (
                <span className="tabular muted">{tidy(entry.distance_miles)} mi</span>
              )}
              <span className="record-when">{when(entry.happened_at)}</span>
              {canWrite && (
                <button
                  type="button"
                  className="icon-button"
                  onClick={async () => {
                    await deleteActivity(entry.id)
                    await refresh()
                  }}
                  aria-label={`Delete the ${entry.kind}`}
                >
                  ×
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

// --- sharing ------------------------------------------------------------------

function Sharing({
  me,
  members,
  canWrite,
}: {
  readonly me: CurrentUser
  readonly members: readonly HouseholdMember[]
  readonly canWrite: boolean
}) {
  const [viewers, setViewers] = useState<string[]>([])
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    void listHealthShares().then((result) => {
      if (result.ok) setViewers(result.data.map((share) => share.viewer_id))
    })
  }, [])

  const others = members.filter((member) => member.id !== me.id)

  return (
    <section className="sharing">
      <h2>Who can see your health records</h2>
      <p className="field-hint">
        Nobody, until you say so &mdash; and only you can change this list, for your own records.
        Removing somebody takes effect immediately. Administrators get no access from being
        administrators.
      </p>

      {others.length === 0 ? (
        <p className="empty">There is nobody else in the household yet.</p>
      ) : (
        <ul className="share-list">
          {others.map((member) => (
            <li key={member.id}>
              <label className="check">
                <input
                  type="checkbox"
                  checked={viewers.includes(member.id)}
                  disabled={!canWrite}
                  onChange={async (event) => {
                    const next = event.currentTarget.checked
                      ? [...viewers, member.id]
                      : viewers.filter((id) => id !== member.id)
                    setViewers(next)
                    setSaved(false)
                    const result = await setHealthShares(next)
                    if (result.ok) setSaved(true)
                  }}
                />
                <MemberMark member={member} size="sm" />
                {member.display_name}
              </label>
            </li>
          ))}
        </ul>
      )}

      {saved && (
        <p className="notice" role="status">
          Saved.
        </p>
      )}
    </section>
  )
}
