import { Icon, Orbit } from './Artwork'
import { CandidateCard } from './CandidateCard'
import { investigationFixtures, scenarioLabels, type MockScenario, type PreviewState } from '../mocks/investigationFixtures'

export function ResultsPanel({ state, onChange }: { state: PreviewState; onChange: (value: PreviewState) => void }) {
  const result = state === 'idle' ? undefined : investigationFixtures[state]
  const candidates = result?.candidates ?? []
  return <aside className="results-panel" aria-label="Search results">
    <div className="panel-heading"><span className="eyebrow">Findings</span><Icon name="globe" /></div>
    {!result ? <div className="results-empty"><Orbit /><h2>Sources, with context.</h2><p>Add a reference photo and subject details to preview the flow.</p><div className="sample-entry"><button className="text-button" onClick={() => onChange('many')}>View sample <Icon name="arrow" /></button><small>No live search</small></div></div>
      : <div className="findings-content">
        <div className="sample-notice"><span className="status-dot" /> Sample · fictional data · no upload</div>
        <label className="scenario-picker">Sample scenario<select value={state} onChange={event => onChange(event.target.value as MockScenario)}>{Object.entries(scenarioLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        <div className="finding-status" role="status">
          <h2>{result.status === 'queued' ? 'Queued' : result.status === 'running' ? 'Review in progress' : result.status === 'failed' ? 'Search failed' : candidates.length ? `${candidates.length} sample finding${candidates.length === 1 ? '' : 's'}` : 'No findings'}</h2>
          {(result.status === 'queued' || result.status === 'running') && <p>Sample state only. No search is running.</p>}
          {result.status === 'completed' && !candidates.length && <p>No candidates returned. This does not mean the person has no public presence.</p>}
        </div>
        {result.status === 'failed' && <div className="findings-error" role="alert"><p>{result.error}</p><button className="text-button" onClick={() => onChange('many')}>Retry sample <Icon name="reset" /></button></div>}
        {Boolean(result.warnings?.length) && <p className="findings-warning" role="status">Some sources are unavailable. These findings are incomplete.</p>}
        {candidates.length > 0 && <><p className="evidence-note">Contextual relevance does not verify a person’s identity.</p><div className="candidate-list">{candidates.map((candidate, index) => <CandidateCard key={`${candidate.id}-${index}`} candidate={candidate} />)}</div></>}
        <button className="text-button reset-results" onClick={() => onChange('idle')}>Close sample <Icon name="close" /></button>
      </div>}
    <div className="results-footnote"><Icon name="shield" /><span>Check each source. Unknown is not low risk.</span></div>
  </aside>
}
