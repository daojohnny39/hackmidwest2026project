import CameraCapture from './CameraCapture'
import ManualCapture from './ManualCapture'

export default function CaptureCheckpointApp() {
  const manual = new URLSearchParams(window.location.search).get('mode') === 'manual'
  return (
    <main className="app">
      <a href="./">← Back to CyberPI</a>
      <h1>{manual ? 'Manual camera checkpoint' : 'Automatic capture'}</h1>
      {manual ? <ManualCapture /> : <CameraCapture />}
    </main>
  )
}
