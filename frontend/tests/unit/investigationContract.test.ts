import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createSearchFormData, parseInvestigation, parseSearchAccepted, safePublicUrl, validateSubject, MAX_IMAGE_BYTES } from '../../src/services/investigationContract.ts'

const subject = { firstName: '  Sample  ', lastName: '', city: '', age: '', authorized: true, externalProcessing: true }
const jpeg = new Blob(['sample bytes'], { type: 'image/jpeg' })

test('requires authorization and a supported photo, while subject details are optional', () => {
  assert.deepEqual(Object.keys(validateSubject({ ...subject, firstName: ' ', authorized: false })).sort(), ['authorized', 'image'])
  assert.deepEqual(validateSubject({ ...subject, firstName: ' ' }, jpeg), {})
  assert.ok(validateSubject(subject, new Blob([], { type: 'image/jpeg' })).image)
  assert.ok(validateSubject(subject, new Blob(['x'], { type: 'image/svg+xml' })).image)
  assert.deepEqual(validateSubject(subject, jpeg), {})
  assert.deepEqual(validateSubject(subject, new Blob(['x'], { type: 'image/png' })), {})
  assert.deepEqual(Object.keys(validateSubject({ ...subject, externalProcessing: false }, jpeg)), ['externalProcessing'])
})
test('enforces draft image and field limits', () => {
  assert.ok(validateSubject(subject, new Blob([new Uint8Array(MAX_IMAGE_BYTES + 1)], { type: 'image/jpeg' })).image)
  for (const key of ['firstName', 'lastName', 'city'] as const) assert.ok(validateSubject({ ...subject, [key]: 'x'.repeat(121) }, jpeg)[key])
  for (const age of ['-1', '1.5', 'abc', 'Infinity']) assert.ok(validateSubject({ ...subject, age }, jpeg).age)
  for (const age of ['', '0', '32']) assert.deepEqual(validateSubject({ ...subject, age }, jpeg), {})
})
test('multipart contract trims details, omits empty optionals, and uses the original Blob', async () => {
  const form = createSearchFormData(subject, jpeg)
  assert.equal(form.get('firstName'), 'Sample')
  assert.equal(form.get('authorized'), 'true')
  assert.equal(form.get('externalProcessing'), 'true')
  for (const key of ['lastName', 'city', 'age']) assert.equal(form.has(key), false)
  const image = form.get('image') as File
  assert.equal(image.name, 'reference.jpg')
  assert.equal(image.type, 'image/jpeg')
  assert.equal(await image.text(), await jpeg.text())
  assert.throws(() => createSearchFormData({ ...subject, authorized: false }, jpeg))
  assert.throws(() => createSearchFormData({ ...subject, externalProcessing: false }, jpeg))
  const unnamed = createSearchFormData({ ...subject, firstName: ' ' }, jpeg)
  assert.equal(unnamed.has('firstName'), false)
  const optional = createSearchFormData({ ...subject, lastName: ' Person ', city: ' Kansas City ', age: ' 32 ' }, jpeg)
  assert.equal(optional.get('lastName'), 'Person')
  assert.equal(optional.get('city'), 'Kansas City')
  assert.equal(optional.get('age'), '32')
})
test('unknown statuses and malformed completion never become successful empty results', () => {
  for (const value of [null, {}, { investigationId: 'test', status: 'maybe' }, { investigationId: 'test', status: ['completed'] }]) {
    assert.throws(() => parseSearchAccepted(value))
  }
  assert.throws(() => parseInvestigation({ investigationId: 'test', status: 'completed' }))
  assert.throws(() => parseInvestigation({ investigationId: 'test', status: 'completed', candidates: {} }))
  assert.deepEqual(parseInvestigation({ investigationId: 'test', status: 'completed', candidates: [] }).candidates, [])
})
test('running stages accept known codes and tolerate older or unfamiliar responses', () => {
  const running = { investigationId: 'test', status: 'running' }
  for (const stage of ['preparing', 'searching', 'comparing', 'reviewing', 'checking', 'finalizing']) {
    assert.equal(parseInvestigation({ ...running, stage }).stage, stage)
  }
  for (const stage of [undefined, null, {}, ['searching'], 'INTERNAL_TRACE_DO_NOT_SHOW']) {
    assert.equal(parseInvestigation({ ...running, stage }).stage, undefined)
  }
  for (const status of ['queued', 'completed', 'failed']) {
    assert.equal(parseInvestigation({ ...running, status, candidates: [], stage: 'searching' }).stage, undefined)
  }
})
test('missing metadata and unfamiliar levels stay unknown and reviewable', () => {
  const result = parseInvestigation({ investigationId: 'test', status: 'completed', candidates: [null, { relevance: { classification: 99, reasons: [' Public context ', 123] }, threat: { level: 'safe' } }, {}] })
  assert.equal(result.candidates?.length, 2)
  assert.deepEqual(result.candidates?.[0].relevance, { classification: 'unknown', reasons: ['Public context'], reviewRequired: true })
  assert.equal(result.candidates?.[0].threat?.level, 'unknown')
  assert.equal(result.candidates?.[1].sourceUrl, undefined)
  assert.equal(result.warnings?.length, 1)
})
test('accepts HTTP(S) source links; rejects executable, malformed, or credential-bearing links', () => {
  for (const url of ['javascript:alert(1)', 'data:text/html,test', 'file:///tmp/test', '/relative', 'https://user:password@example.com/']) assert.equal(safePublicUrl(url), undefined)
  assert.equal(safePublicUrl('https://example.com/profile'), 'https://example.com/profile')
  const result = parseInvestigation({ investigationId: 'test', status: 'completed', candidates: [{ imageUrl: 'data:text/html,test', sourceUrl: 'javascript:alert(1)' }] })
  assert.equal(result.candidates?.[0].imageUrl, undefined)
  assert.equal(result.candidates?.[0].sourceUrl, undefined)
})
test('preserves partial candidates but does not expose raw backend errors or warnings', () => {
  const result = parseInvestigation({ investigationId: 'test', status: 'failed', candidates: [{ id: 'available' }], error: 'INTERNAL_TRACE_DO_NOT_SHOW', warnings: ['INTERNAL_TRACE_DO_NOT_SHOW'] })
  assert.equal(result.status, 'failed')
  assert.equal(result.candidates?.length, 1)
  assert.equal(JSON.stringify(result).includes('INTERNAL_TRACE_DO_NOT_SHOW'), false)
})
test('face-match confidence keeps known levels and a 0-100 score only', () => {
  const result = parseInvestigation({ investigationId: 'test', status: 'completed', candidates: [
    { confidence: { score: 97, level: 'likely', reasons: ['Face similarity 97/100 from Amazon Rekognition (likely match).'] } },
    { confidence: { score: 140, level: 'certain', reasons: 'not a list' } },
    { confidence: { score: Number.NaN, level: 'weak' } },
    { relevance: { classification: 'high', reasons: [] } },
  ] })
  assert.deepEqual(result.candidates?.[0].confidence, { score: 97, level: 'likely', reasons: ['Face similarity 97/100 from Amazon Rekognition (likely match).'] })
  assert.deepEqual(result.candidates?.[1].confidence, { score: undefined, level: 'unknown', reasons: [] })
  assert.deepEqual(result.candidates?.[2].confidence, { score: undefined, level: 'weak', reasons: [] })
  assert.equal(result.candidates?.[3].confidence, undefined)
  assert.equal(result.candidates?.[0].threat, undefined)
})
test('threat keeps a 0-100 score', () => {
  const result = parseInvestigation({ investigationId: 'test', status: 'completed', candidates: [
    { threat: { score: 100, level: 'high', indicators: ['Dev mode marks evilassociation.hakr as a malicious fraud site.'] } },
    { threat: { score: -5, level: 'low', indicators: [] } },
    { threat: { level: 'unknown', indicators: [] } },
  ] })
  assert.deepEqual(result.candidates?.[0].threat, { score: 100, level: 'high', indicators: ['Dev mode marks evilassociation.hakr as a malicious fraud site.'] })
  assert.deepEqual(result.candidates?.[1].threat, { score: undefined, level: 'low', indicators: [] })
  assert.equal(result.candidates?.[2].threat?.score, undefined)
  assert.equal(result.candidates?.[2].threat?.level, 'unknown')
})
