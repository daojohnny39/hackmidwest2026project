import { useEffect, useState, type ReactNode } from 'react'
import CameraCapture from '../CameraCapture'
import type { FrontCaptureResult } from '../capture/types'
import type { PreviewState } from '../mocks/investigationFixtures'
import { ConsentDialog } from './ConsentDialog'
import { ManualStudio } from './ManualStudio'
import { ResultsPanel } from './ResultsPanel'
import { SubjectForm } from './SubjectForm'

export function Studio() {
  const [mode, setMode] = useState(() => new URLSearchParams(window.location.search).get('capture') === 'manual' ? 'manual' : 'automatic')
  const [mirrored, setMirrored] = useState(true)
  const previewOptions = <div className="preview-options" role="group" aria-label="Camera options">
    <label className="mirror-control"><input type="checkbox" checked={mirrored} onChange={event => setMirrored(event.target.checked)} /> Mirror preview</label>
    <label className="mirror-control"><input type="checkbox" checked={mode === 'automatic'} onChange={event => setMode(event.target.checked ? 'automatic' : 'manual')} aria-describedby="capture-mode-hint" /> Automatic capture</label>
    <small id="capture-mode-hint">Switching modes clears the current session.</small>
  </div>
  return mode === 'automatic'
    ? <AutomaticStudio previewOptions={previewOptions} mirrored={mirrored} />
    : <ManualStudio previewOptions={previewOptions} mirrored={mirrored} />
}

function AutomaticStudio({ previewOptions, mirrored }: { previewOptions: ReactNode; mirrored: boolean }) {
  const [reference, setReference] = useState<FrontCaptureResult | null>(null)
  const [results, setResults] = useState<PreviewState>('idle')
  const [session, setSession] = useState(0)
  const [pendingStart, setPendingStart] = useState<(() => void) | null>(null)

  useEffect(() => {
    const clearOnExit = () => {
      setReference(null)
      setResults('idle')
      setPendingStart(null)
      setSession(value => value + 1)
    }
    window.addEventListener('pagehide', clearOnExit)
    return () => window.removeEventListener('pagehide', clearOnExit)
  }, [])

  function clearSession() {
    setReference(null)
    setResults('idle')
    setPendingStart(null)
    // Remounting disposes the previous engine, camera, download URLs and form.
    setSession(value => value + 1)
  }

  return <>
    <section className="studio-intro">
      <div><h1>Public sources. <em>Clearer context.</em></h1></div>
    </section>
    <div className="workspace-grid">
      <div className="capture-column">
        <section className="camera-panel automatic-studio" aria-label="Camera studio">
          <CameraCapture
            key={session}
            previewOptions={previewOptions}
            mirrorPreview={mirrored}
            onStartRequested={start => setPendingStart(() => start)}
            onCaptured={photo => { setReference(photo); setResults('idle') }}
            onDelete={() => { setReference(null); setResults('idle') }}
          />
        </section>
        <SubjectForm key={session} photo={reference?.image} onPreview={() => setResults('many')} onClear={clearSession} />
      </div>
      <ResultsPanel state={results} onChange={setResults} />
    </div>
    {pendingStart && <ConsentDialog onAccept={() => { const start = pendingStart; setPendingStart(null); start() }} onClose={() => setPendingStart(null)} />}
  </>
}
