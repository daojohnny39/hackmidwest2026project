import { useState } from 'react'
import { useCamera } from './hooks/useCamera'

interface Snapshot {
  id: string
  url: string
  takenAt: Date
}

function App() {
  const { videoRef, status, error, devices, activeDeviceId, start, stop, capture } = useCamera()
  const [snapshots, setSnapshots] = useState<Snapshot[]>([])

  const isActive = status === 'active'

  async function takeSnapshot() {
    const blob = await capture()
    const takenAt = new Date()
    setSnapshots((prev) => [{ id: crypto.randomUUID(), url: URL.createObjectURL(blob), takenAt }, ...prev])
  }

  function removeSnapshot(snapshot: Snapshot) {
    URL.revokeObjectURL(snapshot.url)
    setSnapshots((prev) => prev.filter((s) => s.id !== snapshot.id))
  }

  return (
    <main className="app">
      <h1>Camera</h1>

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

        <button type="button" className="primary" onClick={takeSnapshot} disabled={!isActive}>
          Take photo
        </button>
      </div>

      {snapshots.length > 0 && (
        <ul className="snapshots">
          {snapshots.map((snapshot) => (
            <li key={snapshot.id}>
              <img src={snapshot.url} alt={`Photo taken ${snapshot.takenAt.toLocaleTimeString()}`} />
              <div className="snapshot-actions">
                <a href={snapshot.url} download={`photo-${snapshot.takenAt.getTime()}.jpg`}>
                  Download
                </a>
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
