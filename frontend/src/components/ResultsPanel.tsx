import { Icon, Orbit } from './Artwork'
import { CandidateCard } from './CandidateCard'
import { investigationFixtures, scenarioLabels, type MockScenario, type PreviewState } from '../mocks/investigationFixtures'
import type { LiveSearch } from '../hooks/useInvestigation'
import type { SearchStage } from '../types/investigation'

const SEARCH_STAGES: Record<SearchStage, { heading: string; detail: string }> = {
  preparing: { heading: 'Preparing your photo', detail: 'Getting your reference photo ready for the search.' },
  searching: { heading: 'Searching public images', detail: 'Waiting for image search services to return possible matches.' },
  comparing: { heading: 'Comparing faces', detail: 'Checking how closely each result matches your reference photo.' },
  reviewing: { heading: 'Reviewing sources', detail: 'Reading matching public pages and checking their context.' },
  checking: { heading: 'Checking source safety', detail: 'Checking each matching source\'s website for known threats.' },
  finalizing: { heading: 'Preparing findings', detail: 'Organizing matching sources with their confidence and threat levels.' },
}

export function ResultsPanel({ state, onChange, live = { phase: 'idle' }, onCheckAgain }: {
  state: PreviewState; onChange: (value: PreviewState) => void; live?: LiveSearch; onCheckAgain?: (investigationId: string) => void
}) {
  const result = state === 'idle' ? undefined : investigationFixtures[state]
  const candidates = result?.candidates ?? []
  return <aside className="results-panel" aria-label="Search results">
    <div className="panel-heading"><span className="eyebrow">Findings</span><Icon name="globe" /></div>
    {live.phase !== 'idle' ? <LiveFindings search={live} onCheckAgain={onCheckAgain} />
      : !result ? <div className="results-empty"><Orbit /><h2>Sources, with context.</h2><p>Capture a reference photo and search to find public pages that use your face.</p><div className="sample-entry"><button className="text-button" onClick={() => onChange('many')}>View sample <Icon name="arrow" /></button><small>Fictional data</small></div></div>
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
        {candidates.length > 0 && <><p className="evidence-note">Confidence reflects contextual evidence and does not verify a person’s identity.</p><div className="candidate-list">{candidates.map((candidate, index) => <CandidateCard key={`${candidate.id}-${index}`} candidate={candidate} />)}</div></>}
        <button className="text-button reset-results" onClick={() => onChange('idle')}>Close sample <Icon name="close" /></button>
      </div>}
    <div className="results-footnote"><Icon name="shield" /><span>Check each source. Unknown is not low risk.</span></div>
  </aside>
}

function LiveFindings({ search, onCheckAgain }: { search: Exclude<LiveSearch, { phase: 'idle' }>; onCheckAgain?: (investigationId: string) => void }) {
  const result = search.phase === 'waiting' || search.phase === 'done' ? search.result : undefined
  const candidates = result?.candidates ?? []
  const active = search.phase === 'submitting' || result?.status === 'queued' || result?.status === 'running'
  const stage = result?.status === 'running' && result.stage ? SEARCH_STAGES[result.stage] : undefined
  const flagged = candidates.filter(candidate => candidate.threat?.level === 'high' || candidate.threat?.level === 'medium').length
  const checked = candidates.some(candidate => candidate.threat)
  const heading = search.phase === 'submitting' ? 'Sending photo'
    : search.phase === 'error' ? 'Search status unknown'
      : result?.status === 'queued' ? 'Queued'
        : result?.status === 'running' ? stage?.heading ?? 'Searching'
          : result?.status === 'failed' ? 'Search failed'
            : candidates.length ? `${candidates.length} matching source${candidates.length === 1 ? '' : 's'}` : 'No matching sources'
  return <div className="findings-content">
    <div className="finding-status" role="status" aria-live="polite" aria-atomic="true">
      {active && <span className="loading-ring search-loading" aria-hidden="true" />}
      <h2>{heading}</h2>
      {active && <p>{search.phase === 'submitting' ? 'Uploading your reference photo.'
        : result?.status === 'queued' ? 'Waiting for an available search slot.'
          : stage?.detail ?? 'The search is running. Waiting for the next update.'}</p>}
      {result?.status === 'completed' && !candidates.length && <p>No public page with a matching face was found. Search engines miss many pages, so this is not proof that none exist.</p>}
      {result?.status === 'completed' && checked && <p className={flagged ? 'threat-summary flagged' : 'threat-summary'}>{flagged
        ? `${flagged} of these sources ${flagged === 1 ? 'is' : 'are'} flagged as a threat. Open them with care.`
        : 'None of these sources is flagged as a threat.'}</p>}
    </div>
    {search.phase === 'error' && <div className="findings-error" role="alert"><p>{search.message}</p>
      {search.investigationId && onCheckAgain && <button className="text-button" onClick={() => onCheckAgain(search.investigationId!)}>Check again <Icon name="reset" /></button>}</div>}
    {result?.status === 'failed' && <div className="findings-error" role="alert"><p>{result.error}</p></div>}
    {Boolean(result?.warnings?.length) && <p className="findings-warning" role="status">Some sources are unavailable. These findings are incomplete.</p>}
    {candidates.length > 0 && <><p className="evidence-note">Confidence is how closely the face in each source matches your photo, scored by Amazon Rekognition. It does not prove that a source shows you. Threat rates the website, not the person pictured.</p><div className="candidate-list">{candidates.map((candidate, index) => <CandidateCard key={`${candidate.id}-${index}`} candidate={candidate} />)}</div></>}
  </div>
}
