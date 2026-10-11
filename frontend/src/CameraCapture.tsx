import './cameraCapture.css'
import { useEffect, useRef, useState } from 'react'
import { useAutoCapture } from './hooks/useAutoCapture'
import { guidance, REASON_LABELS, troubleshooting } from './capture/guidance'
import type { FrontCaptureResult, ResetReason } from './capture/types'

function CapturePreview({ result, onDelete }: { result: FrontCaptureResult; onDelete: () => void }) {
  const image = useRef<HTMLImageElement>(null)
  const photoLink = useRef<HTMLAnchorElement>(null)
  const metadataLink = useRef<HTMLAnchorElement>(null)
  useEffect(() => {
    const photo = URL.createObjectURL(result.image)
    const metadata = URL.createObjectURL(new Blob([JSON.stringify(result.metadata, null, 2)], { type: 'application/json' }))
    if (image.current) image.current.src = photo
    if (photoLink.current) photoLink.current.href = photo
    if (metadataLink.current) metadataLink.current.href = metadata
    return () => { URL.revokeObjectURL(photo); URL.revokeObjectURL(metadata) }
  }, [result])
  return <>
    <img ref={image} alt="Captured front-facing photo, unmirrored" />
    <p>One face · {result.metadata.image.width} × {result.metadata.image.height} · {Math.round(result.metadata.qualification.holdMs)} ms qualifying hold</p>
    <div className="snapshot-actions">
      <a ref={photoLink} download={`front-${result.metadata.captureId}.jpg`}>Download photo</a>
      <a ref={metadataLink} download={`front-${result.metadata.captureId}.json`}>Download capture metadata</a>
      <button type="button" onClick={onDelete}>Delete photo</button>
    </div>
  </>
}

interface CameraCaptureProps {
  onCaptured?: (result: FrontCaptureResult) => void
  onDelete?: () => void
  onStartRequested?: (start: () => void) => void
}

export default function CameraCapture({ onCaptured, onDelete, onStartRequested }: CameraCaptureProps = {}) {
  const capture = useAutoCapture(onCaptured)
  const { state, result, camera } = capture
  const { videoRef, devices, activeDeviceId, status: cameraStatus, error: cameraError } = camera
  const [aspect, setAspect] = useState(4 / 3)
  const [mirrored, setMirrored] = useState(true)
  const active = cameraStatus === 'active'
  const running = ['preparing', 'guiding', 'holding', 'capturing'].includes(state.phase)
  const message = guidance(state)

  function requestStart() {
    const start = () => capture.start()
    if (onStartRequested) onStartRequested(start)
    else start()
  }

  function deletePhoto() {
    capture.deleteResult()
    onDelete?.()
  }

  function downloadDiagnostics() {
    const blob = new Blob([JSON.stringify({ schemaVersion: 1, sessionId: state.sessionId, preset: state.activeTarget, ...state.diagnostics }, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `front-diagnostics-${state.sessionId ?? 'idle'}.json`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  return (
    <section className="capture-view" aria-label="Front-facing automatic capture">
      <p className="intro">Start the camera, then follow the guidance. A photo is taken automatically once your angle and position pass the checks for 400 ms.</p>
      <div className="guidance-panel" data-running={running}>
        <p className="guidance" role="status">{state.error ? (cameraError ?? state.error.message) : message}</p>
        <progress aria-label="Qualifying hold" value={state.holdProgress} max={1} />
      </div>
      <div className="stage auto-stage" hidden={state.phase === 'complete'} style={{ aspectRatio: aspect, maxWidth: `${aspect * 44}svh` }}>
        <video ref={videoRef} autoPlay playsInline muted className={mirrored ? 'mirrored' : ''} onLoadedMetadata={(event) => {
          const video = event.currentTarget
          if (video.videoWidth && video.videoHeight) setAspect(video.videoWidth / video.videoHeight)
        }} />
        {active && <div className={`face-guide ${state.phase === 'holding' ? 'qualified' : ''}`} aria-hidden="true" />}
        {!active && <div className="placeholder">{state.phase === 'preparing' ? 'Starting camera…' : state.phase === 'complete' ? 'Photo saved · camera off' : 'Camera is off'}</div>}
      </div>
      <div className="controls">
        {running ? <>
          <button type="button" onClick={capture.stop}>Stop camera</button>
          <button type="button" onClick={capture.pause}>Pause capture</button>
        </> : state.phase === 'paused' ? <>
          <button type="button" className="primary" onClick={capture.resume}>Resume capture</button>
          <button type="button" onClick={capture.stop}>Stop camera</button>
        </> : <button type="button" className="primary" onClick={requestStart}>
          {state.phase === 'error' ? 'Try again' : result || state.phase === 'complete' ? 'Retake front photo' : 'Start camera'}
        </button>}
        {devices.length > 1 && <select aria-label="Camera" value={activeDeviceId ?? ''} disabled={state.phase === 'preparing'} onChange={(event) => capture.changeCamera(event.target.value)}>
          {!activeDeviceId && <option value="">Choose a camera</option>}
          {devices.map((device, i) => <option key={device.deviceId} value={device.deviceId}>{device.label || `Camera ${i + 1}`}</option>)}
        </select>}
        <label className="mirror-control"><input type="checkbox" checked={mirrored} onChange={(event) => setMirrored(event.target.checked)} /> Mirror preview</label>
      </div>
      <p className="privacy">Use your own face or a consenting participant. Photos stay on this device.</p>
      {state.troubleshooting && state.phase !== 'complete' && <aside className="troubleshooting" aria-label="Capture troubleshooting">
        <h2>Having trouble capturing?</h2>
        <p>{state.diagnostics.failedHoldCount >= 3 ? 'Your qualifying hold has restarted at least three times.' : 'Capture has been waiting for at least 10 seconds.'}</p>
        <ul>{troubleshooting(state).map((tip) => <li key={tip}>{tip}</li>)}</ul>
        <p>You can pause, adjust your setup, and resume. No photo is taken unless every check passes.</p>
      </aside>}
      <details className="diagnostics">
        <summary>Capture diagnostics · {state.diagnostics.resetCount} hold resets</summary>
        <p>Front view · target 0° on each axis · ±5° tolerance · 400 ms hold. Angles are estimates.</p>
        {state.pose && <p>Estimated yaw {state.pose.yaw.toFixed(1)}° · pitch {state.pose.pitch.toFixed(1)}° · roll {state.pose.roll.toFixed(1)}°</p>}
        {state.faceBox && <p>Face height {(state.faceBox.height * 100).toFixed(1)}% · center {((state.faceBox.x + state.faceBox.width / 2) * 100).toFixed(1)}%, {((state.faceBox.y + state.faceBox.height / 2) * 100).toFixed(1)}%</p>}
        <p>{state.sampleCount} qualifying samples · {Math.round(state.holdElapsedMs)} ms</p>
        {state.failedChecks.length > 0 && <p>Current checks: {state.failedChecks.map((reason) => REASON_LABELS[reason]).join(', ')}</p>}
        <ul>{(Object.keys(state.diagnostics.resetCounts) as ResetReason[]).map((reason) => <li key={reason}>{REASON_LABELS[reason]}: {state.diagnostics.resetCounts[reason]}</li>)}</ul>
        <p>Only interrupted holds count as resets. Each reset records every failing check; the latest 50 events are retained. Counts last for this session.</p>
        <button type="button" onClick={downloadDiagnostics}>Download reset log</button>
      </details>
      {result && <article className="front-result" aria-label="Captured front photo">
        <h2>Captured front photo</h2>
        {running && <p>Your previous photo stays available until the retake succeeds.</p>}
        <CapturePreview result={result} onDelete={deletePhoto} />
      </article>}
    </section>
  )
}
