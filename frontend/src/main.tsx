import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

async function mountApp() {
  const mode = new URLSearchParams(window.location.search).get('mode')
  const checkpoint = mode === 'front' || mode === 'manual'
  // Keep each page's global styles in its own import branch in production builds.
  const [{ default: App }] = checkpoint
    ? await Promise.all([import('./CaptureCheckpointApp'), import('./checkpoint.css')])
    : await Promise.all([import('./App'), import('./index.css')])
  createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
}
void mountApp()
