export const FRONT_PRESET = Object.freeze({
  version: 'front-v1',
  target: Object.freeze({ yaw: 0, pitch: 0, roll: 0 }),
  tolerance: Object.freeze({ yaw: 5, pitch: 5, roll: 5 }),
  holdMs: 400,
  minSamples: 3,
  maxGapMs: 250,
  maxAgeMs: 250,
  intervalMs: 100,
  inferenceMaxEdge: 640,
  jpegQuality: 0.92,
  framing: Object.freeze({ margin: 0.04, minHeight: 0.35, maxHeight: 0.75, minCenter: 0.4, maxCenter: 0.6 }),
  troubleshootingResets: 3,
  troubleshootingMs: 10_000,
  maxResetEvents: 50,
})

export interface Pose { yaw: number; pitch: number; roll: number }
export interface Point { x: number; y: number; z?: number }
export interface Matrix { rows: number; columns: number; data: readonly number[] }
export interface Observation { faces: Point[][]; matrices: Matrix[] }
export interface FaceBox { x: number; y: number; width: number; height: number }
export type GateReason = 'no_face' | 'multiple_faces' | 'invalid_landmarks' | 'clipped' | 'too_close' | 'too_far' | 'off_center' | 'invalid_pose' | 'yaw' | 'pitch' | 'roll'
export type ResetReason = GateReason | 'stale_result' | 'sample_gap' | 'paused' | 'page_hidden' | 'stopped' | 'camera_changed' | 'camera_lost' | 'analysis_error' | 'encoding_error'
export type Phase = 'idle' | 'preparing' | 'guiding' | 'holding' | 'capturing' | 'complete' | 'paused' | 'error'
export type GuidanceCode = ResetReason | 'idle' | 'preparing' | 'hold' | 'capturing' | 'complete'
export interface GateResult {
  pose: Pose | null
  faceBox: FaceBox | null
  failedChecks: GateReason[]
}
export interface ResetEvent {
  atMs: number
  reasons: ResetReason[]
  frameId: string | null
  holdMs: number
  sampleCount: number
}
export interface Diagnostics {
  resetCount: number
  failedHoldCount: number
  resetCounts: Partial<Record<ResetReason, number>>
  blockedCounts: Partial<Record<ResetReason, number>>
  recentResets: ResetEvent[]
}
export interface CaptureState {
  phase: Phase
  sessionId: string | null
  activeTarget: typeof FRONT_PRESET
  pose: Pose | null
  faceBox: FaceBox | null
  failedChecks: ResetReason[]
  guidanceCode: GuidanceCode
  holdElapsedMs: number
  holdProgress: number
  sampleCount: number
  diagnostics: Diagnostics
  troubleshooting: boolean
  error: { code: 'model_error' | 'camera_error' | 'analysis_error' | 'encoding_error'; message: string } | null
}
export interface FrontCaptureResult {
  image: Blob
  metadata: {
    schemaVersion: 1
    sessionId: string
    captureId: string
    frameId: string
    view: 'front'
    capturedAt: string
    image: { mimeType: 'image/jpeg'; width: number; height: number }
    poseDegrees: Pose
    targetDegrees: Pose
    toleranceDegrees: Pose
    qualification: { holdMs: number; sampleCount: number; faceCount: 1; framingPassed: true }
    diagnostics: Diagnostics
    presetVersion: string
    modelVersion: string
    sdkVersion: string
  }
}

/** Both methods must refer to the same immutable source pixels. */
export interface CaptureFrame {
  id: string
  sequence: number
  sampledAt: number
  capturedAt: string
  width: number
  height: number
  analyze: () => Observation | Promise<Observation>
  encode: () => Promise<Blob>
}

export function emptyState(): CaptureState {
  return {
    phase: 'idle', sessionId: null, activeTarget: FRONT_PRESET, pose: null, faceBox: null,
    failedChecks: [], guidanceCode: 'idle', holdElapsedMs: 0, holdProgress: 0, sampleCount: 0,
    diagnostics: { resetCount: 0, failedHoldCount: 0, resetCounts: {}, blockedCounts: {}, recentResets: [] },
    troubleshooting: false, error: null,
  }
}
