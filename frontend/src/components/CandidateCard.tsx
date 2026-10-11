import { useState } from 'react'
import type { CandidateImage, ConfidenceLevel, EvidenceLevel } from '../types/investigation'
import { safePublicUrl } from '../services/investigationContract'
import { Icon } from './Artwork'
function CandidateThumbnail({ url }: { url?: string }) {
  const [failed, setFailed] = useState(false)
  // This local illustration belongs to the mock fixture, never to remote data.
  const src = url === '/demo/source-placeholder.svg' ? url : safePublicUrl(url)
  return <div className="candidate-thumbnail">{src && !failed
    ? <img src={src} alt="Source thumbnail" loading="lazy" referrerPolicy="no-referrer" onError={() => setFailed(true)} />
    : <span><Icon name="camera" /><span>Image unavailable</span></span>}</div>
}
// Confidence badges reuse the evidence styles; threat badges have their own, where high is the warning color.
const CONFIDENCE_TONE: Record<ConfidenceLevel, EvidenceLevel> = { strong: 'high', likely: 'high', possible: 'medium', weak: 'low', unlikely: 'low', unknown: 'unknown' }
export function CandidateCard({ candidate }: { candidate: CandidateImage }) {
  const source = safePublicUrl(candidate.sourceUrl)
  const hostname = source ? new URL(source).hostname : 'Source unavailable'
  const relevance = candidate.relevance?.classification ?? 'unknown'
  const confidence = candidate.confidence
  const reasons = (confidence ?? candidate.relevance)?.reasons ?? []
  const threat = candidate.threat
  return <article className="candidate-card" aria-label={candidate.pageTitle ?? 'Untitled source'}>
    <div className="candidate-top"><CandidateThumbnail key={candidate.imageUrl ?? 'none'} url={candidate.imageUrl} /><div>
      <span className="tiny-label source-host">{hostname}</span><h3>{candidate.pageTitle ?? 'Untitled source'}</h3>
      {candidate.sourceType && <p className="tiny-label">{candidate.sourceType.replaceAll('_', ' ')}</p>}
    </div></div>
    <div className="candidate-labels">{confidence
      ? <span className={`evidence-badge evidence-${CONFIDENCE_TONE[confidence.level]}`}>Confidence: {confidence.score === undefined ? confidence.level : `${confidence.score}% · ${confidence.level} match`}</span>
      : <span className={`evidence-badge evidence-${relevance}`}>Confidence: {relevance}</span>}
      <span className={`evidence-badge${threat ? ` threat-${threat.level}` : ''}`}>Threat: {!threat ? 'not assessed' : threat.score === undefined ? threat.level : `${threat.score}% · ${threat.level}`}</span>
      <span className="review-label">{candidate.relevance?.reviewRequired !== false ? 'Review needed' : 'Not identity-verified'}</span>
    </div>
    {reasons.length ? <ul className="candidate-reasons">{reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul> : <p className="candidate-unknown">No explanation provided.</p>}
    <div className="candidate-bottom">
      {source ? <a href={source} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer" aria-label={`Open source: ${candidate.pageTitle ?? hostname}`}>Open source <Icon name="arrow" /></a> : <span className="tiny-label">No source link</span>}</div>
    {Boolean(threat?.indicators.length) && <details className="risk-details"><summary>Threat details <Icon name="plus" /></summary><ul>{threat!.indicators.map((indicator, index) => <li key={index}>{indicator}</li>)}</ul>
      <p>Threat rates the website, not the person pictured.</p></details>}
  </article>
}
