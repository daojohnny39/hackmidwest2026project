import { useEffect, useRef, useState } from 'react'
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
function releaseSnapshot(photo: Snapshot) {
  URL.revokeObjectURL(photo.url)
  if (photo.overlayUrl) URL.revokeObjectURL(photo.overlayUrl)
}
const prompts = ['Face forward.', 'Turn slightly left.', 'Turn slightly right.', 'Try another angle.', 'Glasses on or off.', 'Take one more photo.']
export function Studio() {
  const camera = useCamera()
  const [consentOpen, setConsentOpen] = useState(false)
  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const snapshotsRef = useRef(snapshots)
  const [results, setResults] = useState<PreviewState>('idle')
  const [formVersion, setFormVersion] = useState(0)
  const [captureError, setCaptureError] = useState<string | null>(null)
  const [capturing, setCapturing] = useState(false)
  const [videoReady, setVideoReady] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const sessionRef = useRef(0)
  const operationRef = useRef(0)
  const analyzerRef = useRef<PhotoAnalyzer | null>(null)
  const captureBusyRef = useRef(false)
  const isActive = camera.status === 'active'
  const selectedPhoto = snapshots.find(photo => photo.id === selectedId) ?? snapshots.at(-1)
  function releasePhotos() { snapshotsRef.current.forEach(releaseSnapshot); snapshotsRef.current = []; setSnapshots([]); setSelectedId(null) }
  function resetAnalysis() {
    operationRef.current++
    analyzerRef.current?.close()
    analyzerRef.current = null
    captureBusyRef.current = false
    setCapturing(false)
  }
  function clearSession() { sessionRef.current++; camera.stop(); resetAnalysis(); releasePhotos(); setResults('idle'); setFormVersion(value => value + 1); setCaptureError(null); setVideoReady(false) }
  useEffect(() => () => {
    sessionRef.current++
    operationRef.current++
    analyzerRef.current?.close()
    analyzerRef.current = null
    snapshotsRef.current.forEach(releaseSnapshot)
    snapshotsRef.current = []
  }, [])
  useEffect(() => {
    const onPageHide = () => {
      sessionRef.current++; camera.stop()
      operationRef.current++; analyzerRef.current?.close(); analyzerRef.current = null
      captureBusyRef.current = false; setCapturing(false)
      snapshotsRef.current.forEach(releaseSnapshot); snapshotsRef.current = []
      setSnapshots([]); setSelectedId(null); setResults('idle'); setFormVersion(value => value + 1); setVideoReady(false)
    }
    window.addEventListener('pagehide', onPageHide)
    return () => window.removeEventListener('pagehide', onPageHide)
  }, [camera.stop])
  function openCamera() { setConsentOpen(false); setCaptureError(null); setVideoReady(false); void camera.start() }
  function stopCamera() { sessionRef.current++; camera.stop(); setVideoReady(false) }
  function updateSnapshot(id: string, update: Partial<Snapshot>) {
    const next = snapshotsRef.current.map(photo => photo.id === id ? { ...photo, ...update } : photo)
    snapshotsRef.current = next
    setSnapshots(next)
  }
  async function analyzePhoto(photo: Snapshot, analyzer: PhotoAnalyzer) {
    const isCurrent = () => analyzerRef.current === analyzer && snapshotsRef.current.some(item => item.id === photo.id)
    try {
      const { report, overlay } = await analyzer.analyze(photo.blob, photo.id)
      // Clearing a session or deleting a photo must never restore late results.
      if (!isCurrent()) return
      updateSnapshot(photo.id, { state: 'complete', report, overlayUrl: URL.createObjectURL(overlay), error: undefined })
    } catch (error) {
      if (isCurrent()) updateSnapshot(photo.id, { state: 'error', error: error instanceof Error ? error.message : 'Could not analyze this photo.' })
    }
  }
  async function takePhoto() {
    if (captureBusyRef.current || snapshotsRef.current.length >= 6) return
    captureBusyRef.current = true; setCapturing(true); setCaptureError(null)
    const session = sessionRef.current
    const operation = ++operationRef.current
    try {
      const blob = await camera.capture()
      if (session !== sessionRef.current || operation !== operationRef.current) return
      const photo: Snapshot = { id: crypto.randomUUID(), blob, url: URL.createObjectURL(blob), state: 'analyzing' }
      const next = [...snapshotsRef.current, photo]
      snapshotsRef.current = next; setSnapshots(next)
      setSelectedId(photo.id)
      if (next.length === 6) stopCamera()
      const analyzer = analyzerRef.current ?? (analyzerRef.current = new PhotoAnalyzer())
      await analyzePhoto(photo, analyzer)
    } catch (error) { if (session === sessionRef.current) setCaptureError(error instanceof Error ? error.message : 'Could not take this photo. Please try again.') }
    finally { if (operation === operationRef.current) { captureBusyRef.current = false; setCapturing(false) } }
  }
  async function retryAnalysis(photo: Snapshot) {
    if (captureBusyRef.current || !snapshotsRef.current.some(item => item.id === photo.id)) return
    captureBusyRef.current = true; setCapturing(true)
    const operation = ++operationRef.current
    updateSnapshot(photo.id, { state: 'analyzing', error: undefined })
    const analyzer = analyzerRef.current ?? (analyzerRef.current = new PhotoAnalyzer())
    try { await analyzePhoto(photo, analyzer) }
    finally { if (operation === operationRef.current) { captureBusyRef.current = false; setCapturing(false) } }
  }
  function deletePhoto(id: string) {
    const photo = snapshotsRef.current.find(item => item.id === id)
    if (photo) releaseSnapshot(photo)
    const next = snapshotsRef.current.filter(item => item.id !== id)
    snapshotsRef.current = next; setSnapshots(next)
  }
  return <>
    <section className="studio-intro"><div><h1>Public sources. <em>Clearer context.</em></h1><p>Capture a reference. Review the evidence.</p></div><span className="session-badge"><span className="status-dot" /> Local preview</span></section>
    <div className="workspace-grid"><div className="capture-column"><section className="camera-panel" aria-label="Camera studio">
      <div className={`camera-stage ${isActive ? 'is-live' : ''}`}><video ref={camera.videoRef} autoPlay playsInline muted className={isActive ? 'is-active' : ''} onLoadedData={() => setVideoReady(true)} onEmptied={() => setVideoReady(false)} aria-label="Live camera preview" aria-hidden={!isActive} />
        <div className="camera-heading"><span className="eyebrow recognition-text">Face capture</span><span className={`camera-status ${isActive ? 'active' : ''}`}><span className="status-dot" /> {isActive ? 'Live' : camera.status === 'starting' ? 'Connecting' : 'Camera off'}</span></div>
        {!isActive && <div className="camera-idle"><FaceArtwork /><div className="idle-copy"><h2>{camera.status === 'starting' ? 'Allow camera access' : snapshots.length ? 'Review your photos' : 'Ready to begin'}</h2></div></div>}
        {isActive && <div className="live-guidance"><div className="viewfinder" aria-hidden="true" /><span>{prompts[Math.min(snapshots.length, 5)]}</span></div>}
      </div>
      <div className="camera-toolbar"><p>{isActive ? 'Use good lighting.' : snapshots.length >= 6 ? 'Capture complete.' : 'Your consent comes first.'}</p>
        {isActive ? <div className="capture-controls"><button className="icon-button" onClick={stopCamera} aria-label="Stop camera"><Icon name="stop" /></button><button className="button primary" onClick={takePhoto} disabled={!videoReady || capturing || snapshots.length >= 6}><Icon name="camera" />{capturing ? 'Analyzing…' : 'Take photo'}</button></div> : camera.status === 'starting' ? <button className="button secondary" onClick={stopCamera}>Cancel <Icon name="close" /></button> : <button className="button primary" onClick={() => setConsentOpen(true)} disabled={snapshots.length >= 6}><Icon name="camera" />{camera.status === 'error' ? 'Retry camera' : snapshots.length >= 6 ? 'Photos ready' : 'Open camera'}</button>}
      </div>
      {(camera.error || captureError) && <p className="camera-error" role="alert">{captureError || camera.error}</p>}
      {isActive && camera.devices.length > 1 && <label className="camera-picker">Camera<select value={camera.activeDeviceId ?? ''} onChange={event => { sessionRef.current++; setVideoReady(false); void camera.start(event.target.value) }}>{camera.devices.map((device, index) => <option value={device.deviceId} key={device.deviceId}>{device.label || `Camera ${index + 1}`}</option>)}</select></label>}
      <div className="photo-tray"><div className="tray-heading"><span className="eyebrow">Photos <span className="count" role="status">{snapshots.length} / 6</span></span><span>Only in this tab</span></div><div className="photo-slots">{Array.from({ length: 6 }, (_, index) => <div className={`photo-slot ${snapshots[index] ? 'filled' : ''}`} key={snapshots[index]?.id ?? index}>{snapshots[index] ? <><button className="photo-select" onClick={() => setSelectedId(snapshots[index].id)} aria-label={`Review photo ${index + 1}`} aria-pressed={selectedPhoto?.id === snapshots[index].id}><img src={snapshots[index].url} alt={`Captured photo ${index + 1}`} /></button><button className="photo-delete" onClick={() => deletePhoto(snapshots[index].id)} aria-label={`Delete photo ${index + 1}`}><Icon name="close" /></button></> : <><Icon name={index === 0 ? 'camera' : 'plus'} /><span>0{index + 1}</span></>}</div>)}</div>{selectedPhoto && <PhotoReview key={selectedPhoto.id} photo={selectedPhoto} number={snapshots.indexOf(selectedPhoto) + 1} busy={capturing} onRetry={() => void retryAnalysis(selectedPhoto)} />}</div>
    </section><SubjectForm key={formVersion} photo={selectedPhoto?.blob} photoNumber={selectedPhoto ? snapshots.indexOf(selectedPhoto) + 1 : undefined} onPreview={() => setResults('many')} onClear={clearSession} /></div><ResultsPanel state={results} onChange={setResults} /></div>
    {consentOpen && <ConsentDialog onAccept={openCamera} onClose={() => setConsentOpen(false)} />}
  </>
}
