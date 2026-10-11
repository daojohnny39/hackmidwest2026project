import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

async function mountApp() {
  const mode = new URLSearchParams(window.location.search).get('mode')
  const checkpoint = mode === 'front' || mode === 'manual'
  const [{ default: App }] = await Promise.all([
    checkpoint ? import('./CaptureCheckpointApp') : import('./App'),
    checkpoint ? import('./checkpoint.css') : import('./index.css'),
  ])
  createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
}
void mountApp()
