import { FaceLandmarker, FilesetResolver, type FaceLandmarkerResult } from '@mediapipe/tasks-vision'

export interface PhotoAnalysis {
  frame_id: string
  image_width: number
  image_height: number
  face_count: number
  max_faces: number
  landmarks_per_face: number[]
  face_landmarks: { x: number; y: number; z: number }[][]
  // Preserve the web runtime's matrix representation without assuming its layout.
  facial_transformation_matrixes: { rows: number; columns: number; data: number[] }[]
}

function makeReport(result: FaceLandmarkerResult, frameId: string, width: number, height: number): PhotoAnalysis {
  return {
    frame_id: frameId,
    image_width: width,
    image_height: height,
    face_count: result.faceLandmarks.length,
    max_faces: 2,
    landmarks_per_face: result.faceLandmarks.map((face) => face.length),
    face_landmarks: result.faceLandmarks.map((face) => face.map(({ x, y, z }) => ({ x, y, z }))),
    facial_transformation_matrixes: result.facialTransformationMatrixes.map(({ rows, columns, data }) => ({
      rows, columns, data: Array.from(data),
    })),
  }
}

// DOM image decoding works across Safari, Firefox, and Chromium without depending
// on Blob -> ImageBitmap or OffscreenCanvas implementations.
async function decodePhoto(blob: Blob): Promise<HTMLImageElement> {
  const url = URL.createObjectURL(blob)
  const image = new Image()
  try {
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve()
      image.onerror = () => reject(new Error('This browser could not decode the captured photo.'))
      image.src = url
    })
    return image
  } finally {
    image.onload = null
    image.onerror = null
    URL.revokeObjectURL(url)
  }
}

async function makeOverlay(image: HTMLImageElement, report: PhotoAnalysis): Promise<Blob> {
  const canvas = document.createElement('canvas')
  canvas.width = image.naturalWidth
  canvas.height = image.naturalHeight
  const context = canvas.getContext('2d')
  if (!context) throw new Error('This browser could not draw the landmark overlay.')
  context.drawImage(image, 0, 0)
  const colors = ['#00ff00', '#00c8ff']
  const radius = Math.max(1, Math.round(Math.min(canvas.width, canvas.height) / 700))
  report.face_landmarks.forEach((landmarks, index) => {
    context.fillStyle = colors[index % colors.length]
    for (const { x, y } of landmarks) {
      if (x < 0 || x > 1 || y < 0 || y > 1) continue
      context.beginPath()
      context.arc(x * (canvas.width - 1), y * (canvas.height - 1), radius, 0, 2 * Math.PI)
      context.fill()
    }
  })
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error('Could not save the landmark overlay.')), 'image/png')
  })
}

/** One lazily loaded detector per camera screen, reused for sequential photos. */
export class PhotoAnalyzer {
  private detector: FaceLandmarker | null = null
  private closed = false
  private busy = false

  async analyze(blob: Blob, frameId: string): Promise<{ report: PhotoAnalysis; overlay: Blob }> {
    if (this.closed) throw new Error('Photo analysis has stopped.')
    if (this.busy) throw new Error('Wait for the current photo to finish analyzing.')
    this.busy = true
    let image: HTMLImageElement | undefined
    try {
      image = await decodePhoto(blob)
      if (this.closed) throw new Error('Photo analysis has stopped.')
      if (!this.detector) {
        try {
          const assetRoot = `${import.meta.env.BASE_URL}mediapipe`
          const files = await FilesetResolver.forVisionTasks(`${assetRoot}/wasm`)
          if (this.closed) throw new Error('Photo analysis has stopped.')
          const detector = await FaceLandmarker.createFromOptions(files, {
            canvas: document.createElement('canvas'),
            baseOptions: { modelAssetPath: `${assetRoot}/face_landmarker.task`, delegate: 'CPU' },
            runningMode: 'IMAGE',
            numFaces: 2,
            outputFacialTransformationMatrixes: true,
            outputFaceBlendshapes: false,
          })
          if (this.closed) {
            detector.close()
            throw new Error('Photo analysis has stopped.')
          }
          this.detector = detector
        } catch (cause) {
          throw new Error('Face analysis could not load. Try again, or refresh the page.', { cause })
        }
      }
      const result = this.detector.detect(image)
      const report = makeReport(result, frameId, image.naturalWidth, image.naturalHeight)
      const overlay = await makeOverlay(image, report)
      return { report, overlay }
    } finally {
      image?.removeAttribute('src')
      this.busy = false
    }
  }

  close() {
    this.closed = true
    this.detector?.close()
    this.detector = null
  }
}
