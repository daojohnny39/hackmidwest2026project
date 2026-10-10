import { useEffect, useRef, useState } from 'react'
import { useCamera } from './hooks/useCamera'
import { PhotoAnalyzer, type PhotoAnalysis } from './faceAnalysis'

interface Snapshot {
  id: string
  url: string
  takenAt: Date
  blob: Blob
  state: 'analyzing' | 'complete' | 'error'
  report?: PhotoAnalysis
  overlayUrl?: string
  reportUrl?: string
  error?: string
}

function SnapshotPreview({ snapshot }: { snapshot: Snapshot }) {
  const [showLandmarks, setShowLandmarks] = useState(true)
  return (
    <>
      <img
        src={showLandmarks && snapshot.overlayUrl ? snapshot.overlayUrl : snapshot.url}
        alt={`Photo taken ${snapshot.takenAt.toLocaleTimeString()}${showLandmarks && snapshot.overlayUrl ? ' with facial landmarks' : ''}`}
      />
      {snapshot.overlayUrl && (
        <button type="button" onClick={() => setShowLandmarks((shown) => !shown)}>
          {showLandmarks ? 'Show original' : 'Show landmarks'}
        </button>
      )}
    </>
  )
}

function App() {
  const { videoRef, status, error, devices, activeDeviceId, start, stop, capture } = useCamera()
  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const [isAnalyzing, setIsAnalyzing] = useState(false)
  const [captureError, setCaptureError] = useState<string | null>(null)
  const analyzerRef = useRef<PhotoAnalyzer | null>(null)
  const busyRef = useRef(false)
  const snapshotIds = useRef(new Set<string>())
  const objectUrls = useRef(new Set<string>())

  useEffect(() => {
    const analyzer = new PhotoAnalyzer()
    analyzerRef.current = analyzer
    const ids = snapshotIds.current
    const urls = objectUrls.current
    return () => {
      analyzerRef.current = null
      analyzer.close()
      ids.clear()
      urls.forEach((url) => URL.revokeObjectURL(url))
      urls.clear()
    }
  }, [])

  const isActive = status === 'active'

  function makeUrl(blob: Blob) {
    const url = URL.createObjectURL(blob)
    objectUrls.current.add(url)
    return url
  }

  async function analyzeSnapshot(snapshot: Snapshot) {
    const analyzer = analyzerRef.current
    if (!analyzer) return
    try {
      const { report, overlay } = await analyzer.analyze(snapshot.blob, snapshot.id)
      if (!snapshotIds.current.has(snapshot.id)) return
      const overlayUrl = makeUrl(overlay)
      const reportUrl = makeUrl(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }))
      setSnapshots((prev) => prev.map((item) => item.id === snapshot.id
        ? { ...item, state: 'complete', report, overlayUrl, reportUrl, error: undefined }
        : item))
    } catch (cause) {
      if (!snapshotIds.current.has(snapshot.id)) return
      const message = cause instanceof Error ? cause.message : 'Could not analyze this photo.'
      setSnapshots((prev) => prev.map((item) => item.id === snapshot.id
        ? { ...item, state: 'error', error: message }
        : item))
    }
  }

  async function takeSnapshot() {
    if (busyRef.current) return
    busyRef.current = true
    setIsAnalyzing(true)
    setCaptureError(null)
    try {
      const blob = await capture()
      if (!analyzerRef.current) return
      const snapshot: Snapshot = {
        id: crypto.randomUUID(), url: makeUrl(blob), takenAt: new Date(), blob, state: 'analyzing',
      }
      snapshotIds.current.add(snapshot.id)
      setSnapshots((prev) => [snapshot, ...prev])
      await analyzeSnapshot(snapshot)
    } catch (cause) {
      if (analyzerRef.current) setCaptureError(cause instanceof Error ? cause.message : 'Could not take a photo.')
    } finally {
      busyRef.current = false
      if (analyzerRef.current) setIsAnalyzing(false)
    }
  }

  async function retryAnalysis(snapshot: Snapshot) {
    if (busyRef.current) return
    busyRef.current = true
    setIsAnalyzing(true)
    setSnapshots((prev) => prev.map((item) => item.id === snapshot.id
      ? { ...item, state: 'analyzing', error: undefined }
      : item))
    try {
      await analyzeSnapshot(snapshot)
    } finally {
      busyRef.current = false
      if (analyzerRef.current) setIsAnalyzing(false)
    }
  }

  function removeSnapshot(snapshot: Snapshot) {
    snapshotIds.current.delete(snapshot.id)
    for (const url of [snapshot.url, snapshot.overlayUrl, snapshot.reportUrl]) {
      if (!url) continue
      URL.revokeObjectURL(url)
      objectUrls.current.delete(url)
    }
    setSnapshots((prev) => prev.filter((s) => s.id !== snapshot.id))
  }

  return (
    <main className="app">
      <h1>Camera</h1>
      <p className="intro">Take a photo to check facial landmarks on this device.</p>

      <div className="stage">
        <video ref={videoRef} autoPlay playsInline muted />
        {!isActive && (
          <div className="placeholder">
            {status === 'starting' ? 'Starting camera…' : (error ?? 'Camera is off')}
          </div>
        )}
      </div>

      <div className="controls">
        {isActive ? (
          <button type="button" onClick={stop}>
            Stop camera
          </button>
        ) : (
          <button type="button" onClick={() => start()} disabled={status === 'starting'}>
            {status === 'error' ? 'Try again' : 'Start camera'}
          </button>
        )}

        {devices.length > 1 && (
          <select
            aria-label="Camera"
            value={activeDeviceId ?? ''}
            onChange={(e) => start(e.target.value)}
            disabled={status === 'starting'}
          >
            {!activeDeviceId && <option value="">Choose a camera</option>}
            {devices.map((device, i) => (
              <option key={device.deviceId} value={device.deviceId}>
                {device.label || `Camera ${i + 1}`}
              </option>
            ))}
          </select>
        )}

        <button type="button" className="primary" onClick={takeSnapshot} disabled={!isActive || isAnalyzing}>
          {isAnalyzing ? 'Analyzing…' : 'Take photo'}
        </button>
      </div>

      {captureError && <p className="error" role="alert">{captureError}</p>}

      {snapshots.length > 0 && (
        <ul className="snapshots">
          {snapshots.map((snapshot) => (
            <li key={snapshot.id} data-frame-id={snapshot.id}>
              <SnapshotPreview snapshot={snapshot} />
              <div className="analysis-status" aria-live="polite">
                {snapshot.state === 'analyzing' && <p>Analyzing photo… The first photo may take a moment.</p>}
                {snapshot.state === 'error' && <p className="error">{snapshot.error}</p>}
                {snapshot.report && (
                  <>
                    <p><strong>{snapshot.report.face_count} {snapshot.report.face_count === 1 ? 'face' : 'faces'} detected</strong></p>
                    <p>{snapshot.report.landmarks_per_face.join(', ') || '0'} landmarks · {snapshot.report.facial_transformation_matrixes.length} transformation {snapshot.report.facial_transformation_matrixes.length === 1 ? 'matrix' : 'matrices'}</p>
                    {snapshot.report.face_count === 0 && <p>Try a closer, well-lit view of your face.</p>}
                    {snapshot.report.face_count > 1 && <p>Multiple faces detected. Use one person for the checkpoint.</p>}
                  </>
                )}
              </div>
              {snapshot.state === 'error' && (
                <button type="button" onClick={() => retryAnalysis(snapshot)} disabled={isAnalyzing}>Retry analysis</button>
              )}
              <div className="snapshot-actions">
                <a href={snapshot.url} download={`photo-${snapshot.takenAt.getTime()}.jpg`}>
                  Download photo
                </a>
                {snapshot.overlayUrl && <a href={snapshot.overlayUrl} download={`landmarks-${snapshot.id}.png`}>Download landmarks</a>}
                {snapshot.reportUrl && <a href={snapshot.reportUrl} download={`analysis-${snapshot.id}.json`}>Download analysis</a>}
                <button type="button" onClick={() => removeSnapshot(snapshot)}>
                  Delete
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </main>
  )
}

export default App
