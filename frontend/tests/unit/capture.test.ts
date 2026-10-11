import { test } from 'node:test'
import assert from 'node:assert/strict'
import { FrontCaptureEngine } from '../../src/capture/engine.ts'
import { matrixToPose } from '../../src/capture/pose.ts'
import { evaluateFront } from '../../src/capture/gates.ts'
import { guidance, troubleshooting } from '../../src/capture/guidance.ts'
import { FRONT_PRESET, type CaptureFrame, type FrontCaptureResult, type Observation } from '../../src/capture/types.ts'
import { matrix, observation } from './fixtures.ts'

function harness() {
  let now = 0, sequence = 0
  const captures: FrontCaptureResult[] = []
  const engine = new FrontCaptureEngine('test-session', { onState: () => {}, onCaptured: (r) => captures.push(r) }, () => now)
  engine.ready()
  const frame = (time: number, data = observation(), overrides: Partial<CaptureFrame> = {}): CaptureFrame => {
    now = time
    const id = `frame-${++sequence}`
    return { id, sequence, sampledAt: time, capturedAt: '2026-10-10T12:00:00.000Z', width: 1280, height: 720,
      analyze: () => data, encode: async () => new Blob([id], { type: 'image/jpeg' }), ...overrides }
  }
  return { engine, captures, frame, setTime: (time: number) => { now = time } }
}

for (const pose of [{ yaw: 0, pitch: 0, roll: 0 }, { yaw: -35, pitch: 20, roll: -18 }, { yaw: 25, pitch: -12, roll: 15 }]) {
  test(`column-major combined rotation and scale: ${JSON.stringify(pose)}`, () => {
    const result = matrixToPose(matrix(pose, 1.7))!
    for (const axis of ['yaw', 'pitch', 'roll'] as const) assert.ok(Math.abs(result[axis] - pose[axis]) < 1e-10)
  })
}
test('rejects invalid dimensions, NaNs, perspective, shear, reflections, and singular poses', () => {
  assert.equal(matrixToPose(undefined), null)
  assert.equal(matrixToPose({ ...matrix(), rows: 3 }), null)
  for (const [index, value] of [[0, NaN], [3, .1], [0, -1], [4, .2], [0, 0]]) {
    const m = matrix(); m.data = [...m.data]; (m.data as number[])[index] = value
    assert.equal(matrixToPose(m), null)
  }
  assert.equal(matrixToPose(matrix({ yaw: 89 })), null)
})
test('every angle uses the fixed inclusive ±5° limits and full precision', () => {
  for (const axis of ['yaw', 'pitch', 'roll']) for (const sign of [-1, 1]) {
    assert.deepEqual(evaluateFront(observation({ [axis]: sign * 5 })).failedChecks, [])
    assert.deepEqual(evaluateFront(observation({ [axis]: sign * 5.001 })).failedChecks, [axis])
  }
  assert.deepEqual(evaluateFront(observation({ yaw: 6, pitch: 6, roll: -6 })).failedChecks, ['yaw', 'pitch', 'roll'])
})
test('face count, framing, and invalid estimates block capture in priority order', () => {
  assert.deepEqual(evaluateFront({ faces: [], matrices: [] }).failedChecks, ['no_face'])
  const two = observation(); two.faces.push(two.faces[0])
  assert.deepEqual(evaluateFront(two).failedChecks, ['multiple_faces'])
  for (const [box, failure] of [
    [{ x: 0, y: .1, width: .8, height: .8 }, 'clipped'],
    [{ x: .3, y: .1, width: .4, height: .8 }, 'too_close'],
    [{ x: .4, y: .4, width: .2, height: .2 }, 'too_far'],
    [{ x: .1, y: .225, width: .4, height: .55 }, 'off_center'],
  ] as const) assert.equal(evaluateFront(observation({}, box)).failedChecks[0], failure)
  const bad = observation(); bad.matrices = []
  assert.deepEqual(evaluateFront(bad).failedChecks, ['invalid_pose'])
  bad.faces[0] = []
  assert.deepEqual(evaluateFront(bad).failedChecks, ['invalid_landmarks', 'invalid_pose'])
})
test('400 ms requires fresh qualifying samples; commits the qualifying frame exactly once', async () => {
  const h = harness()
  for (const t of [0, 100, 200, 300, 399]) await h.engine.process(h.frame(t))
  assert.equal(h.captures.length, 0)
  const last = h.frame(400)
  await h.engine.process(last)
  assert.equal(h.captures.length, 1)
  assert.equal(h.captures[0].metadata.frameId, last.id)
  assert.equal(await h.captures[0].image.text(), last.id)
  assert.equal(h.captures[0].metadata.qualification.holdMs, 400)
  assert.deepEqual(h.captures[0].metadata.toleranceDegrees, { yaw: 5, pitch: 5, roll: 5 })
  await h.engine.process(h.frame(500))
  assert.equal(h.captures.length, 1)
})
test('a timer or duplicated/out-of-order frames cannot complete a hold', async () => {
  const h = harness(), f = h.frame(0)
  await h.engine.process(f)
  h.setTime(200)
  await h.engine.process(f)
  await h.engine.process({ ...f, sampledAt: 100 })
  assert.equal(h.engine.state.sampleCount, 1)
  h.setTime(500); h.engine.tick()
  assert.equal(h.captures.length, 0)
  assert.equal(h.engine.state.holdProgress, 0)
  assert.equal(h.engine.state.diagnostics.resetCounts.sample_gap, 1)
})
test('reset records all failed checks once per interrupted hold and restarts timing', async () => {
  const h = harness()
  for (const t of [0, 100, 200, 300]) await h.engine.process(h.frame(t))
  await h.engine.process(h.frame(399, observation({ yaw: 6, roll: 7 })))
  await h.engine.process(h.frame(400, observation({ yaw: 6 })))
  const log = h.engine.state.diagnostics
  assert.equal(log.resetCount, 1)
  assert.deepEqual(log.recentResets[0].reasons, ['yaw', 'roll'])
  assert.equal(log.recentResets[0].holdMs, 300)
  for (const t of [500, 600, 700, 800]) await h.engine.process(h.frame(t))
  assert.equal(h.captures.length, 0)
  await h.engine.process(h.frame(900))
  assert.equal(h.captures.length, 1)
})
test('sample gaps and stale results never inherit a previous hold', async () => {
  const h = harness()
  await h.engine.process(h.frame(0)); await h.engine.process(h.frame(100))
  await h.engine.process(h.frame(400))
  assert.equal(h.engine.state.holdElapsedMs, 0)
  assert.equal(h.engine.state.diagnostics.resetCounts.sample_gap, 1)
  await h.engine.process(h.frame(500, observation(), { analyze: () => { h.setTime(751); return observation() } }))
  assert.equal(h.engine.state.holdElapsedMs, 0)
  assert.equal(h.engine.state.diagnostics.resetCounts.stale_result, 1)
  assert.equal(h.captures.length, 0)
})
test('troubleshooting after three failed holds; logs bounded; thresholds unchanged', async () => {
  const h = harness()
  for (let i = 0; i < 60; i++) {
    await h.engine.process(h.frame(i * 200))
    await h.engine.process(h.frame(i * 200 + 100, observation({ yaw: 6 })))
    if (i === 1) assert.equal(h.engine.state.troubleshooting, false)
    if (i === 2) assert.equal(h.engine.state.troubleshooting, true)
  }
  assert.equal(h.engine.state.diagnostics.resetCount, 60)
  assert.equal(h.engine.state.diagnostics.recentResets.length, 50)
  assert.equal(FRONT_PRESET.tolerance.yaw, 5)
  assert.ok(troubleshooting(h.engine.state).join(' ').includes('±5°'))
})
test('10 seconds without ever qualifying offers help without fabricated resets', async () => {
  const h = harness()
  await h.engine.process(h.frame(0, { faces: [], matrices: [] }))
  h.setTime(10_000); h.engine.tick()
  assert.equal(h.engine.state.troubleshooting, true)
  assert.equal(h.engine.state.diagnostics.resetCount, 0)
  assert.match(troubleshooting(h.engine.state)[0], /light/)
})
for (const reason of ['paused', 'page_hidden', 'stopped', 'camera_changed', 'camera_lost'] as const) {
  test(`${reason} records interruption and discards a pending encoded image`, async () => {
    const h = harness()
    for (const t of [0, 100, 200, 300]) await h.engine.process(h.frame(t))
    let release!: (blob: Blob) => void
    const pending = h.engine.process(h.frame(400, observation(), { encode: () => new Promise((resolve) => { release = resolve }) }))
    await Promise.resolve()
    assert.equal(h.engine.state.phase, 'capturing')
    h.engine.interrupt(reason)
    release(new Blob(['late'], { type: 'image/jpeg' })); await pending
    assert.equal(h.captures.length, 0)
    assert.equal(h.engine.state.diagnostics.resetCounts[reason], 1)
    assert.equal(h.engine.state.diagnostics.failedHoldCount, 0)
  })
}
test('late analysis from a previous generation cannot unlock or publish into resumed work', async () => {
  const h = harness()
  let oldResult!: (o: Observation) => void, newResult!: (o: Observation) => void
  const old = h.engine.process(h.frame(0, observation(), { analyze: () => new Promise((r) => { oldResult = r }) }))
  h.engine.interrupt('paused'); h.engine.resume(); h.engine.ready()
  const current = h.engine.process(h.frame(100, observation(), { analyze: () => new Promise((r) => { newResult = r }) }))
  oldResult(observation()); await old
  assert.equal(h.engine.canProcess, false)
  assert.equal(h.engine.state.sampleCount, 0)
  newResult(observation()); await current
  assert.equal(h.engine.state.sampleCount, 1)
})
test('encoding failure is actionable and never emits a capture', async () => {
  const h = harness()
  for (const t of [0, 100, 200, 300]) await h.engine.process(h.frame(t))
  await h.engine.process(h.frame(400, observation(), { encode: async () => { throw new Error('No JPEG') } }))
  assert.equal(h.engine.state.error?.code, 'encoding_error')
  assert.equal(h.engine.state.diagnostics.resetCounts.encoding_error, 1)
  assert.equal(h.captures.length, 0)
})
test('consumer failure cannot roll back or duplicate a capture; Blob remains reusable', async () => {
  let now = 0, calls = 0, saved: FrontCaptureResult | null = null
  const engine = new FrontCaptureEngine('consumer', { onState: () => {}, onCaptured: (result) => { saved = result; calls++; throw new Error('Mock downstream failure') } }, () => now)
  engine.ready()
  const h = harness()
  for (const t of [0, 200, 400, 600]) { now = t; await engine.process(h.frame(t)) }
  assert.equal(calls, 1)
  assert.equal(engine.state.phase, 'complete')
  assert.equal(await saved!.image.text(), saved!.metadata.frameId)
})
test('guidance pitch signs describe chin correction; display rounding does not affect gates', () => {
  const h = harness()
  const state = h.engine.state
  state.guidanceCode = 'pitch'
  state.pose = { yaw: 0, pitch: 10, roll: 0 }
  assert.match(guidance(state), /Lower/)
  state.pose.pitch = -10
  assert.match(guidance(state), /Raise/)
})
