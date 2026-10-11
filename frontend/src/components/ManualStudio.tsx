import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useCamera } from '../hooks/useCamera'
import { FaceArtwork, Icon } from './Artwork'
import { ConsentDialog } from './ConsentDialog'
import { ResultsPanel } from './ResultsPanel'
import type { PreviewState } from '../mocks/investigationFixtures'
import { SubjectForm } from './SubjectForm'
import { PhotoAnalyzer, type PhotoAnalysis } from '../faceAnalysis'
import { PhotoReview } from './PhotoReview'

export interface Snapshot {
  id: string
  url: string
  blob: Blob
  state: 'analyzing' | 'complete' | 'error'
  report?: PhotoAnalysis
  overlayUrl?: string
  error?: string
}
function releaseSnapshot(photo: Snapshot | null) {
  if (!photo) return
  URL.revokeObjectURL(photo.url)
  if (photo.overlayUrl) URL.revokeObjectURL(photo.overlayUrl)
}

interface ManualStudioProps { previewOptions: ReactNode; mirrored: boolean }

export function ManualStudio({ previewOptions, mirrored }: ManualStudioProps) {
  const [session, setSession] = useState(0)
  return <ManualSession key={session} previewOptions={previewOptions} mirrored={mirrored} onClear={() => setSession(value => value + 1)} />
}

function ManualSession({ onClear, previewOptions, mirrored }: ManualStudioProps & { onClear: () => void }) {
  const { videoRef, status, error, devices, activeDeviceId, start, stop, capture } = useCamera()
  const [consentOpen, setConsentOpen] = useState(false)
  const [photo, setPhoto] = useState<Snapshot | null>(null)
  const photoRef = useRef<Snapshot | null>(null)
  const [results, setResults] = useState<PreviewState>('idle')
  const [captureError, setCaptureError] = useState<string | null>(null)
  const [capturing, setCapturing] = useState(false)
  const [videoReady, setVideoReady] = useState(false)
  const sessionRef = useRef(0)
  const analyzerRef = useRef<PhotoAnalyzer | null>(null)
  const busyRef = useRef(false)
  const isActive = status === 'active'

  useEffect(() => () => {
    sessionRef.current++
    analyzerRef.current?.close()
    analyzerRef.current = null
    releaseSnapshot(photoRef.current)
    photoRef.current = null
  }, [])

  useEffect(() => {
    const onPageHide = () => { stop(); onClear() }
    window.addEventListener('pagehide', onPageHide)
    return () => window.removeEventListener('pagehide', onPageHide)
  }, [stop, onClear])

  function openCamera() { setConsentOpen(false); setCaptureError(null); setVideoReady(false); void start() }
  function stopCamera() { sessionRef.current++; stop(); setVideoReady(false) }
  function updatePhoto(id: string, update: Partial<Snapshot>) {
    if (photoRef.current?.id !== id) return
    const next = { ...photoRef.current, ...update }
    photoRef.current = next
    setPhoto(next)
  }
  async function analyzePhoto(snapshot: Snapshot) {
    const analyzer = analyzerRef.current ?? (analyzerRef.current = new PhotoAnalyzer())
    try {
      const { report, overlay } = await analyzer.analyze(snapshot.blob, snapshot.id)
      if (analyzerRef.current !== analyzer || photoRef.current?.id !== snapshot.id) return
      updatePhoto(snapshot.id, { state: 'complete', report, overlayUrl: URL.createObjectURL(overlay), error: undefined })
    } catch (cause) {
      if (analyzerRef.current === analyzer) updatePhoto(snapshot.id, { state: 'error', error: cause instanceof Error ? cause.message : 'Could not analyze this photo.' })
    }
  }
  async function takePhoto() {
    if (busyRef.current) return
    busyRef.current = true; setCapturing(true); setCaptureError(null)
    const session = sessionRef.current
    try {
      const blob = await capture()
      if (session !== sessionRef.current) return
      const next: Snapshot = { id: crypto.randomUUID(), blob, url: URL.createObjectURL(blob), state: 'analyzing' }
      releaseSnapshot(photoRef.current)
      photoRef.current = next; setPhoto(next); setResults('idle')
      // One reference completes capture. Retake replaces it only after a new JPEG exists.
      stop(); setVideoReady(false)
      await analyzePhoto(next)
    } catch (cause) {
      if (session === sessionRef.current) setCaptureError(cause instanceof Error ? cause.message : 'Could not take this photo. Please try again.')
    } finally {
      if (session === sessionRef.current) { busyRef.current = false; setCapturing(false) }
    }
  }
  async function retryAnalysis(snapshot: Snapshot) {
    if (busyRef.current || photoRef.current?.id !== snapshot.id) return
    busyRef.current = true; setCapturing(true)
    const session = sessionRef.current
    updatePhoto(snapshot.id, { state: 'analyzing', error: undefined })
    try { await analyzePhoto(snapshot) }
    finally { if (session === sessionRef.current) { busyRef.current = false; setCapturing(false) } }
  }
  function deletePhoto() {
    releaseSnapshot(photoRef.current)
    photoRef.current = null; setPhoto(null); setResults('idle')
  }

  return <>
    <section className="studio-intro"><div><h1>Public sources. <em>Clearer context.</em></h1></div></section>
    <div className="workspace-grid"><div className="capture-column"><section className="camera-panel" aria-label="Camera studio">
      <div className={`camera-stage ${isActive ? 'is-live' : ''}`}><video ref={videoRef} autoPlay playsInline muted className={isActive ? 'is-active' : ''} style={{ transform: mirrored ? 'scaleX(-1)' : 'none' }} onLoadedData={() => setVideoReady(true)} onEmptied={() => setVideoReady(false)} aria-label="Live camera preview" aria-hidden={!isActive} />
        <div className="camera-heading"><span className="eyebrow recognition-text">Face capture</span><span className={`camera-status ${isActive ? 'active' : ''}`}><span className="status-dot" /> {isActive ? 'Live' : status === 'starting' ? 'Connecting' : 'Camera off'}</span></div>
        {!isActive && <div className="camera-idle"><FaceArtwork /><div className="idle-copy"><h2>{status === 'starting' ? 'Allow camera access' : photo ? 'Review your photo' : 'Ready to begin'}</h2></div></div>}
        {isActive && <div className="live-guidance"><div className="viewfinder" aria-hidden="true" /><span>Face forward.</span></div>}
      </div>
      <div className="camera-toolbar"><p>{isActive ? 'Use good lighting.' : photo ? 'Reference photo ready.' : 'Your consent comes first.'}</p>
        {isActive ? <div className="capture-controls"><button className="icon-button" onClick={stopCamera} aria-label="Stop camera"><Icon name="stop" /></button><button className="button primary" onClick={takePhoto} disabled={!videoReady || capturing}><Icon name="camera" />{capturing ? 'Analyzing…' : 'Take photo'}</button></div> : status === 'starting' ? <button className="button secondary" onClick={stopCamera}>Cancel <Icon name="close" /></button> : <button className="button primary" onClick={() => setConsentOpen(true)} disabled={capturing}><Icon name="camera" />{status === 'error' ? 'Retry camera' : photo ? 'Retake photo' : 'Open camera'}</button>}
      </div>
      <div className="manual-preview-options">{previewOptions}</div>
      {(error || captureError) && <p className="camera-error" role="alert">{captureError || error}</p>}
      {isActive && devices.length > 1 && <label className="camera-picker">Camera<select value={activeDeviceId ?? ''} onChange={event => { sessionRef.current++; setVideoReady(false); void start(event.target.value) }}>{devices.map((device, index) => <option value={device.deviceId} key={device.deviceId}>{device.label || `Camera ${index + 1}`}</option>)}</select></label>}
      <div className="photo-tray"><div className="tray-heading"><span className="eyebrow">Reference photo <span className="count">{photo ? '1 / 1' : '0 / 1'}</span></span><span>Only in this tab</span></div>
        <div className="photo-slots single-photo"><div className={`photo-slot ${photo ? 'filled' : ''}`}>{photo ? <><img src={photo.url} alt="Captured photo 1" /><button className="photo-delete" onClick={deletePhoto} aria-label="Delete photo 1"><Icon name="close" /></button></> : <Icon name="camera" />}</div></div>
        {photo && <PhotoReview key={photo.id} photo={photo} number={1} busy={capturing} onRetry={() => void retryAnalysis(photo)} />}
      </div>
    </section><SubjectForm photo={photo?.blob} onPreview={() => setResults('many')} onClear={() => { stop(); onClear() }} /></div><ResultsPanel state={results} onChange={setResults} /></div>
    {consentOpen && <ConsentDialog onAccept={openCamera} onClose={() => setConsentOpen(false)} />}
  </>
}
