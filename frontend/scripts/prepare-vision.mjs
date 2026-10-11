import { cp, mkdir, access, readFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { MODEL_VERSION, SDK_VERSION } from '../src/capture/versions.ts'

const model = new URL('../../models/face_landmarker.task', import.meta.url)
const destination = new URL('../public/mediapipe/', import.meta.url)
try {
  await access(model)
} catch {
  throw new Error('Download models/face_landmarker.task using Step 3 in the project README first.')
}
const digest = createHash('sha256').update(await readFile(model)).digest('hex')
const sdk = JSON.parse(await readFile(new URL('../node_modules/@mediapipe/tasks-vision/package.json', import.meta.url), 'utf8'))
if (`sha256:${digest}` !== MODEL_VERSION || sdk.version !== SDK_VERSION) {
  throw new Error('Capture model/SDK versions do not match the verified preset. Update versions.ts and validate pose conventions before changing assets.')
}
await mkdir(destination, { recursive: true })
await cp(model, new URL('face_landmarker.task', destination))
await cp(
  new URL('../node_modules/@mediapipe/tasks-vision/wasm/', import.meta.url),
  new URL('wasm/', destination),
  { recursive: true },
)
console.log(`Prepared local Face Landmarker assets in ${fileURLToPath(destination)}`)
