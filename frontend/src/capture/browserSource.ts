import { FaceLandmarker, FilesetResolver } from '@mediapipe/tasks-vision'
import { FrontCaptureEngine } from './engine.ts'
import { FRONT_PRESET, type CaptureFrame } from './types.ts'

export class BrowserCaptureSource {
  private video: HTMLVideoElement
  private engine: FrontCaptureEngine
  private detector: FaceLandmarker | null = null
  private closed = false
  private videoCallback: number | null = null
  private animation: number | null = null
  private watchdog: ReturnType<typeof setInterval> | null = null
  private lastSample = -Infinity
  private lastSourceFrame = -1
  private sequence = 0
  private dimensions = ''

  constructor(video: HTMLVideoElement, engine: FrontCaptureEngine) { this.video = video; this.engine = engine }

  async start() {
    try {
      const root = `${import.meta.env.BASE_URL}mediapipe`
      const files = await FilesetResolver.forVisionTasks(`${root}/wasm`)
      if (this.closed) return
      const detector = await FaceLandmarker.createFromOptions(files, {
        canvas: document.createElement('canvas'),
        baseOptions: { modelAssetPath: `${root}/face_landmarker.task`, delegate: 'CPU' },
        runningMode: 'VIDEO', numFaces: 2, outputFacialTransformationMatrixes: true, outputFaceBlendshapes: false,
      })
      if (this.closed) { detector.close(); return }
      this.detector = detector
      this.engine.ready()
      this.watchdog = setInterval(() => this.engine.tick(), 100)
      this.schedule()
    } catch (cause) {
      if (!this.closed) this.engine.fail('model_error', `Face analysis could not load. Check the local model assets and retry. ${cause instanceof Error ? cause.message : ''}`)
    }
  }

  private schedule() {
    if (this.closed) return
    if (this.video.requestVideoFrameCallback) {
      this.videoCallback = this.video.requestVideoFrameCallback((_now, metadata) => {
        this.videoCallback = null
        this.sample(metadata.presentedFrames)
        this.schedule()
      })
    } else {
      this.animation = requestAnimationFrame(() => {
        this.animation = null
        const frames = this.video.getVideoPlaybackQuality?.().totalVideoFrames
        this.sample(frames || this.video.currentTime)
        this.schedule()
      })
    }
  }

  private sample(sourceFrame: number) {
    const video = this.video, detector = this.detector
    const now = performance.now()
    if (this.closed || document.hidden || !detector || !this.engine.canProcess || video.readyState < 2 || !video.videoWidth || video.paused) return
    if (sourceFrame === this.lastSourceFrame || now - this.lastSample < FRONT_PRESET.intervalMs) return
    const dimensions = `${video.videoWidth}x${video.videoHeight}`
    if (this.dimensions && this.dimensions !== dimensions) {
      this.engine.interrupt('camera_changed')
      this.engine.fail('camera_error', 'Camera dimensions changed. Restart capture to use the new orientation.')
      return
    }
    this.dimensions = dimensions
    this.lastSourceFrame = sourceFrame
    this.lastSample = now
    let original: HTMLCanvasElement | undefined
    let inference: HTMLCanvasElement | undefined
    try {
      original = document.createElement('canvas')
      original.width = video.videoWidth
      original.height = video.videoHeight
      const fullContext = original.getContext('2d')
      if (!fullContext) throw new Error('This browser could not freeze the camera frame.')
      fullContext.drawImage(video, 0, 0)
      inference = document.createElement('canvas')
      const scale = Math.min(1, FRONT_PRESET.inferenceMaxEdge / Math.max(original.width, original.height))
      inference.width = Math.round(original.width * scale)
      inference.height = Math.round(original.height * scale)
      const context = inference.getContext('2d')
      if (!context) throw new Error('This browser could not prepare the analysis frame.')
      context.drawImage(original, 0, 0, inference.width, inference.height)
      const frozen = original, analysisImage = inference
      const frame: CaptureFrame = {
        id: crypto.randomUUID(), sequence: ++this.sequence, sampledAt: now, capturedAt: new Date().toISOString(),
        width: original.width, height: original.height,
        analyze: () => {
          const result = detector.detectForVideo(analysisImage, now)
          return { faces: result.faceLandmarks, matrices: result.facialTransformationMatrixes }
        },
        encode: () => new Promise((resolve, reject) => {
          frozen.toBlob((blob) => blob ? resolve(blob) : reject(new Error('Could not encode this frame. Please retry.')), 'image/jpeg', FRONT_PRESET.jpegQuality)
        }),
      }
      void this.engine.process(frame).finally(() => {
        // Only release after the encoder has consumed the exact frozen source.
        frozen.width = frozen.height = analysisImage.width = analysisImage.height = 1
      })
    } catch (cause) {
      if (original) original.width = original.height = 1
      if (inference) inference.width = inference.height = 1
      this.engine.fail('analysis_error', cause instanceof Error ? cause.message : 'Could not analyze the camera frame.')
    }
  }

  close() {
    this.closed = true
    if (this.videoCallback !== null) this.video.cancelVideoFrameCallback(this.videoCallback)
    if (this.animation !== null) cancelAnimationFrame(this.animation)
    if (this.watchdog !== null) clearInterval(this.watchdog)
    this.detector?.close()
    this.detector = null
  }
}
