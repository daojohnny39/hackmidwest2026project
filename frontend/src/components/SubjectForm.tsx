import { useState, type FormEvent } from 'react'
import { FIELD_LIMITS, validateSubject } from '../services/investigationContract'
import type { SubjectInput } from '../types/investigation'
import { Icon } from './Artwork'
export function SubjectForm({ photo, photoNumber, onPreview, onClear }: {
  photo?: Blob; photoNumber?: number; onPreview: () => void; onClear: () => void
}) {
  const [input, setInput] = useState<SubjectInput>({ firstName: '', lastName: '', city: '', age: '', authorized: false })
  const [touched, setTouched] = useState<Partial<Record<keyof SubjectInput, boolean>>>({})
  const errors = validateSubject(input, photo)
  const valid = Object.keys(errors).length === 0
  const touch = (field: keyof SubjectInput) => setTouched(previous => ({ ...previous, [field]: true }))
  function submit(event: FormEvent) {
    event.preventDefault()
    setTouched({ firstName: true, lastName: true, city: true, age: true, authorized: true })
    if (valid) onPreview()
  }
  return <form className="subject-form" onSubmit={submit} noValidate aria-labelledby="subject-heading">
    <div className="form-heading"><div className="subject-title"><h2 id="subject-heading">Subject details</h2><details className="subject-info">
      <summary aria-label="About subject details"><Icon name="info" /></summary>
      <p>Providing information in the fields below helps narrow results. All fields are optional.</p>
    </details></div><span className="tiny-label">Local preview</span></div>
    <div className="optional-fields">
      {([{ key: 'firstName', label: 'First Name' }, { key: 'lastName', label: 'Last Name' }, { key: 'city', label: 'City' }] as const).map(({ key, label }) =>
        <div key={key}><label htmlFor={`subject-${key}`}>{label}</label>
          <input id={`subject-${key}`} name={key} autoComplete="off" value={input[key]} maxLength={FIELD_LIMITS[key]}
            onChange={event => setInput({ ...input, [key]: event.target.value })} onBlur={() => touch(key)}
            aria-invalid={Boolean(touched[key] && errors[key])} aria-describedby={touched[key] && errors[key] ? `${key}-error` : undefined} />
          {touched[key] && errors[key] && <p id={`${key}-error`} className="field-error">{errors[key]}</p>}
        </div>)}
      <div><label htmlFor="subject-age">Age</label>
        <input id="subject-age" name="age" type="number" min="0" step="1" autoComplete="off" value={input.age}
          onChange={event => setInput({ ...input, age: event.target.value })} onBlur={() => touch('age')}
          aria-invalid={Boolean(touched.age && errors.age)} aria-describedby={touched.age && errors.age ? 'age-error' : undefined} />
        {touched.age && errors.age && <p id="age-error" className="field-error">{errors.age}</p>}
      </div>
    </div>
    <label className="authorization-check" htmlFor="subject-authorized"><input id="subject-authorized" type="checkbox" required
      checked={input.authorized} onChange={event => { setInput({ ...input, authorized: event.target.checked }); touch('authorized') }}
      onBlur={() => touch('authorized')} aria-invalid={Boolean(touched.authorized && errors.authorized)}
      aria-describedby={touched.authorized && errors.authorized ? 'authorization-error' : undefined} />
      <span>I am this person, or I have their permission to search their public information.</span></label>
    {touched.authorized && errors.authorized && <p id="authorization-error" className="field-error">{errors.authorized}</p>}
    <p id="reference-hint" className={errors.image && photo ? 'field-error' : 'reference-hint'}>{errors.image ?? `Photo ${photoNumber} selected as the reference.`}</p>
    <div className="form-actions"><button className="button primary" type="submit" disabled={!valid} aria-describedby="reference-hint preview-note">Preview sample findings <Icon name="arrow" /></button>
      <button className="text-button" type="button" onClick={onClear}>Clear session <Icon name="reset" /></button></div>
    <p id="preview-note" className="field-hint">Nothing is uploaded. Sample findings are unrelated to your photo.</p>
  </form>
}
