import { evaluateFront } from './gates.ts'
import { FRONT_PRESET, emptyState, type CaptureFrame, type CaptureState, type FrontCaptureResult, type ResetReason } from './types.ts'
import { MODEL_VERSION, SDK_VERSION } from './versions.ts'

const interruptions = new Set<ResetReason>(['paused', 'page_hidden', 'stopped', 'camera_changed', 'camera_lost'])

/** No camera, DOM, React, or network dependencies. A session commits at most once. */
export class FrontCaptureEngine {
  private current: CaptureState
  private epoch = 0
  private busy = false
  private disposed = false
  private holdStart: number | null = null
  private lastSample: number | null = null
  private lastSequence = -1
  private lastFrameId: string | null = null
  private waitingSince: number
  private startedAt: number
  private listeners: {
    onState: (state: CaptureState) => void
    onCaptured: (result: FrontCaptureResult) => void
    onError?: (error: NonNullable<CaptureState['error']>) => void
  }
  private now: () => number

  constructor(sessionId: string, listeners: FrontCaptureEngine['listeners'], now = () => performance.now()) {
    this.listeners = listeners
    this.now = now
    this.startedAt = this.waitingSince = now()
    this.current = { ...emptyState(), sessionId, phase: 'preparing', guidanceCode: 'preparing' }
  }

  get state(): CaptureState { return structuredClone(this.current) }
  get canProcess(): boolean { return !this.disposed && !this.busy && ['guiding', 'holding'].includes(this.current.phase) }

  private publish() {
    if (!this.disposed) this.notify(() => this.listeners.onState(this.state))
  }

  // Consumer errors cannot roll back a committed capture or generate another one.
  private notify(callback: () => void) {
    try { callback() } catch (error) { console.error('Capture subscriber failed', error) }
  }

  ready() {
    if (this.disposed || this.current.phase !== 'preparing') return
    this.waitingSince = this.now()
    this.current.phase = 'guiding'
    this.current.guidanceCode = 'no_face'
    this.publish()
  }

  private reset(reasons: ResetReason[], at: number, frameId = this.lastFrameId) {
    if (this.holdStart !== null) {
      const d = this.current.diagnostics
      d.resetCount++
      if (reasons.some((r) => !interruptions.has(r))) d.failedHoldCount++
      for (const reason of reasons) d.resetCounts[reason] = (d.resetCounts[reason] ?? 0) + 1
      d.recentResets.push({ atMs: Math.max(0, at - this.startedAt), reasons: [...reasons], frameId, holdMs: this.current.holdElapsedMs, sampleCount: this.current.sampleCount })
      if (d.recentResets.length > FRONT_PRESET.maxResetEvents) d.recentResets.shift()
    }
    this.holdStart = null
    this.current.holdElapsedMs = 0
    this.current.holdProgress = 0
    this.current.sampleCount = 0
  }

  private help(now: number) {
    if (this.current.diagnostics.failedHoldCount >= FRONT_PRESET.troubleshootingResets || now - this.waitingSince >= FRONT_PRESET.troubleshootingMs) this.current.troubleshooting = true
  }

  /** A watchdog only clears evidence; it must never advance a hold or capture. */
  tick() {
    if (this.disposed || !['guiding', 'holding'].includes(this.current.phase)) return
    const now = this.now()
    if (this.holdStart !== null && this.lastSample !== null && now - this.lastSample > FRONT_PRESET.maxGapMs) {
      this.reset(['sample_gap'], now)
      this.current.phase = 'guiding'
      this.current.guidanceCode = 'sample_gap'
      this.current.failedChecks = ['sample_gap']
    }
    this.help(now)
    this.publish()
  }

  interrupt(reason: 'paused' | 'page_hidden' | 'stopped' | 'camera_changed' | 'camera_lost') {
    if (this.disposed || this.current.phase === 'complete') return
    this.epoch++
    this.busy = false
    this.reset([reason], this.now())
    this.current.phase = ['paused', 'page_hidden'].includes(reason) ? 'paused' : 'idle'
    this.current.guidanceCode = reason
    this.current.failedChecks = []
    this.current.pose = null
    this.current.faceBox = null
    this.publish()
  }

  resume() {
    if (this.disposed || !['paused', 'idle', 'error'].includes(this.current.phase)) return
    this.epoch++
    this.lastSample = null
    this.lastSequence = -1
    this.lastFrameId = null
    this.current.phase = 'preparing'
    this.current.guidanceCode = 'preparing'
    this.current.error = null
    this.publish()
  }

  fail(code: NonNullable<CaptureState['error']>['code'], message: string) {
    if (this.disposed || this.current.phase === 'complete') return
    this.epoch++
    this.busy = false
    this.reset([code === 'camera_error' ? 'camera_lost' : code === 'encoding_error' ? 'encoding_error' : 'analysis_error'], this.now())
    this.current.phase = 'error'
    this.current.error = { code, message }
    this.publish()
    this.notify(() => this.listeners.onError?.({ code, message }))
  }

  async process(frame: CaptureFrame): Promise<void> {
    if (!this.canProcess || frame.sequence <= this.lastSequence || (this.lastSample !== null && frame.sampledAt <= this.lastSample)) return
    if (!Number.isFinite(frame.sampledAt) || frame.sampledAt > this.now() || !Number.isInteger(frame.sequence) || frame.width < 1 || frame.height < 1) return
    this.busy = true
    const epoch = this.epoch
    const stillActive = () => !this.disposed && epoch === this.epoch
    const previousSample = this.lastSample
    this.lastSample = frame.sampledAt
    this.lastSequence = frame.sequence
    this.lastFrameId = frame.id
    try {
      const observation = await frame.analyze()
      if (!stillActive()) return
      const now = this.now()
      if (now - frame.sampledAt > FRONT_PRESET.maxAgeMs) {
        this.reset(['stale_result'], now, frame.id)
        this.current.phase = 'guiding'
        this.current.guidanceCode = 'stale_result'
        this.current.failedChecks = ['stale_result']
        this.current.pose = null
        this.current.faceBox = null
        const d = this.current.diagnostics
        d.blockedCounts.stale_result = (d.blockedCounts.stale_result ?? 0) + 1
        this.help(now)
        this.publish()
        return
      }
      const gates = evaluateFront(observation)
      const failures: ResetReason[] = [...gates.failedChecks]
      if (previousSample !== null && frame.sampledAt - previousSample > FRONT_PRESET.maxGapMs) failures.unshift('sample_gap')
      this.current.pose = gates.pose
      this.current.faceBox = gates.faceBox
      this.current.failedChecks = failures
      for (const reason of failures) {
        const d = this.current.diagnostics
        d.blockedCounts[reason] = (d.blockedCounts[reason] ?? 0) + 1
      }
      if (failures.length) this.reset(failures, now, frame.id)
      if (gates.failedChecks.length) {
        this.current.phase = 'guiding'
        this.current.guidanceCode = gates.failedChecks[0]
      } else {
        // A fresh good frame after a gap starts a NEW hold; it never inherits time.
        this.holdStart ??= frame.sampledAt
        this.current.sampleCount++
        this.current.holdElapsedMs = frame.sampledAt - this.holdStart
        this.current.holdProgress = Math.min(1, this.current.holdElapsedMs / FRONT_PRESET.holdMs)
        this.current.phase = 'holding'
        this.current.guidanceCode = 'hold'
      }
      this.help(now)
      if (this.current.phase !== 'holding' || this.current.holdElapsedMs < FRONT_PRESET.holdMs || this.current.sampleCount < FRONT_PRESET.minSamples) {
        this.publish()
        return
      }
      // Reserve the one result BEFORE asynchronous encoding or subscriber code.
      this.current.phase = 'capturing'
      this.current.guidanceCode = 'capturing'
      this.publish()
      let image: Blob
      try {
        image = await frame.encode()
        if (!image.size || image.type !== 'image/jpeg') throw new Error('The camera frame could not be encoded as JPEG.')
      } catch (cause) {
        if (stillActive()) this.fail('encoding_error', cause instanceof Error ? cause.message : 'Could not save the photo. Please retry.')
        return
      }
      if (!stillActive()) return
      const result: FrontCaptureResult = {
        image,
        metadata: {
          schemaVersion: 1, sessionId: this.current.sessionId!, captureId: crypto.randomUUID(), frameId: frame.id,
          view: 'front', capturedAt: frame.capturedAt,
          image: { mimeType: 'image/jpeg', width: frame.width, height: frame.height },
          poseDegrees: { ...gates.pose! }, targetDegrees: { ...FRONT_PRESET.target }, toleranceDegrees: { ...FRONT_PRESET.tolerance },
          qualification: { holdMs: this.current.holdElapsedMs, sampleCount: this.current.sampleCount, faceCount: 1, framingPassed: true },
          diagnostics: structuredClone(this.current.diagnostics), presetVersion: FRONT_PRESET.version, modelVersion: MODEL_VERSION, sdkVersion: SDK_VERSION,
        },
      }
      this.holdStart = null
      this.current.phase = 'complete'
      this.current.guidanceCode = 'complete'
      this.current.failedChecks = []
      this.publish()
      if (stillActive()) this.notify(() => this.listeners.onCaptured(result))
    } catch (cause) {
      if (stillActive()) this.fail('analysis_error', cause instanceof Error ? cause.message : 'Face analysis failed. Please retry.')
    } finally {
      if (stillActive()) this.busy = false
    }
  }

  dispose() { this.disposed = true; this.epoch++; this.busy = false }
}
