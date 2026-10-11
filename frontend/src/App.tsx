import CameraCapture from './CameraCapture'
import ManualCapture from './ManualCapture'

export default function App() {
  const manual = new URLSearchParams(window.location.search).get('mode') === 'manual'
  return (
    <main className="app">
      <h1>Camera</h1>
      {manual ? <ManualCapture /> : <CameraCapture />}
    </main>
  )
}
