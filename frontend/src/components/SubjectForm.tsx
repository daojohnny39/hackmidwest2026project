import { useState, type FormEvent } from 'react'
import { FIELD_LIMITS, validateSubject } from '../services/investigationContract'
import type { SubjectInput } from '../types/investigation'
import { Icon } from './Artwork'
export function SubjectForm({ photo, photoNumber, onPreview, onClear }: {
  photo?: Blob; photoNumber?: number; onPreview: () => void; onClear: () => void
}) {
  const [input, setInput] = useState<SubjectInput>({ name: '', username: '', context: '', authorized: false })
  const [touched, setTouched] = useState<Partial<Record<keyof SubjectInput, boolean>>>({})
  const errors = validateSubject(input, photo)
  const valid = Object.keys(errors).length === 0
  const touch = (field: keyof SubjectInput) => setTouched(previous => ({ ...previous, [field]: true }))
  function submit(event: FormEvent) {
    event.preventDefault()
    setTouched({ name: true, username: true, context: true, authorized: true })
    if (valid) onPreview()
  }
  return <form className="subject-form" onSubmit={submit} noValidate aria-labelledby="subject-heading">
    <div className="form-heading"><h2 id="subject-heading">Subject details</h2><span className="tiny-label">Local preview</span></div>
    <label htmlFor="subject-name">Name <span className="field-hint">required</span></label>
    <input id="subject-name" name="subjectName" autoComplete="off" value={input.name} maxLength={FIELD_LIMITS.name}
      required onChange={event => setInput({ ...input, name: event.target.value })} onBlur={() => touch('name')}
      aria-invalid={Boolean(touched.name && errors.name)} aria-describedby={touched.name && errors.name ? 'name-error' : undefined} />
    {touched.name && errors.name && <p id="name-error" className="field-error">{errors.name}</p>}
    <div className="optional-fields">
      <div><label htmlFor="subject-username">Known username <span className="field-hint">optional</span></label>
        <input id="subject-username" name="username" autoComplete="off" spellCheck={false} value={input.username} maxLength={FIELD_LIMITS.username}
          onChange={event => setInput({ ...input, username: event.target.value })} /></div>
      <div><label htmlFor="subject-context">Public context <span className="field-hint">optional</span></label>
        <input id="subject-context" name="context" autoComplete="off" value={input.context} maxLength={FIELD_LIMITS.context}
          placeholder="e.g. public work or portfolio" onChange={event => setInput({ ...input, context: event.target.value })} /></div>
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
