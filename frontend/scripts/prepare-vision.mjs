import { cp, mkdir, access } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const model = new URL('../../models/face_landmarker.task', import.meta.url)
const destination = new URL('../public/mediapipe/', import.meta.url)
try {
  await access(model)
} catch {
  throw new Error('Download models/face_landmarker.task using Step 3 in the project README first.')
}
await mkdir(destination, { recursive: true })
await cp(model, new URL('face_landmarker.task', destination))
await cp(
  new URL('../node_modules/@mediapipe/tasks-vision/wasm/', import.meta.url),
  new URL('wasm/', destination),
  { recursive: true },
)
console.log(`Prepared local Face Landmarker assets in ${fileURLToPath(destination)}`)
