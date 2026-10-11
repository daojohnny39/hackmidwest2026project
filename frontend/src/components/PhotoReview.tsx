import { useState } from 'react'
import type { Snapshot } from './Studio'

export function PhotoReview({ photo, number, busy, onRetry }: { photo: Snapshot; number: number; busy: boolean; onRetry: () => void }) {
  const [showLandmarks, setShowLandmarks] = useState(true)
  const withLandmarks = showLandmarks && Boolean(photo.overlayUrl)
  return <article className="photo-review" data-frame-id={photo.id} aria-label={`Photo ${number} analysis`}>
    <img src={withLandmarks ? photo.overlayUrl : photo.url} alt={`Photo ${number}${withLandmarks ? ' with facial landmarks' : ''}`} />
    <div className="photo-analysis">
      <span className="tiny-label">Photo {number} · On-device analysis</span>
      <div className="analysis-status" role="status">
        {photo.state === 'analyzing' && <><h3 className="recognition-text">Checking face…</h3><p>The first photo loads the model.</p></>}
        {photo.state === 'error' && <><h3 className="recognition-text">Analysis unavailable</h3><p>{photo.error}</p></>}
        {photo.report && <>
          <h3 className="recognition-text">{photo.report.face_count} {photo.report.face_count === 1 ? 'face' : 'faces'} detected</h3>
          {photo.report.face_count === 1 && <p>{photo.report.landmarks_per_face[0]} landmarks · Face found</p>}
          {photo.report.face_count === 0 && <p>Move closer and use good lighting.</p>}
          {photo.report.face_count > 1 && <p>Use one person in the frame.</p>}
        </>}
      </div>
      {photo.state === 'error' && <button className="text-button" onClick={onRetry} disabled={busy}>Retry analysis</button>}
      {photo.overlayUrl && <button className="text-button" aria-pressed={showLandmarks} onClick={() => setShowLandmarks(shown => !shown)}>{showLandmarks ? 'Show original' : 'Show landmarks'}</button>}
    </div>
  </article>
}
