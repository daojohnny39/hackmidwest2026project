import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createSearchFormData, parseInvestigation, parseSearchAccepted, safePublicUrl, validateSubject, MAX_IMAGE_BYTES } from '../../src/services/investigationContract.ts'

const subject = { name: '  Sample Person  ', username: '', context: '', authorized: true }
const jpeg = new Blob(['sample bytes'], { type: 'image/jpeg' })

test('requires a name, authorization, and a nonempty supported photo', () => {
  assert.deepEqual(Object.keys(validateSubject({ ...subject, name: ' ', authorized: false })).sort(), ['authorized', 'image', 'name'])
  assert.ok(validateSubject(subject, new Blob([], { type: 'image/jpeg' })).image)
  assert.ok(validateSubject(subject, new Blob(['x'], { type: 'image/svg+xml' })).image)
  assert.deepEqual(validateSubject(subject, jpeg), {})
  assert.deepEqual(validateSubject(subject, new Blob(['x'], { type: 'image/png' })), {})
})
test('enforces draft image and field limits', () => {
  assert.ok(validateSubject(subject, new Blob([new Uint8Array(MAX_IMAGE_BYTES + 1)], { type: 'image/jpeg' })).image)
  assert.ok(validateSubject({ ...subject, name: 'n'.repeat(121) }, jpeg).name)
  assert.ok(validateSubject({ ...subject, username: 'u'.repeat(101), context: 'c'.repeat(501) }, jpeg).context)
})
test('multipart contract trims details, omits empty optionals, and uses the original Blob', async () => {
  const form = createSearchFormData(subject, jpeg)
  assert.equal(form.get('name'), 'Sample Person')
  assert.equal(form.get('authorized'), 'true')
  assert.equal(form.has('username'), false)
  assert.equal(form.has('context'), false)
  const image = form.get('image') as File
  assert.equal(image.name, 'reference.jpg')
  assert.equal(image.type, 'image/jpeg')
  assert.equal(await image.text(), await jpeg.text())
  assert.throws(() => createSearchFormData({ ...subject, authorized: false }, jpeg))
  const optional = createSearchFormData({ ...subject, username: ' sample ', context: ' public work ' }, jpeg)
  assert.equal(optional.get('username'), 'sample')
  assert.equal(optional.get('context'), 'public work')
})
test('unknown statuses and malformed completion never become successful empty results', () => {
  for (const value of [null, {}, { investigationId: 'test', status: 'maybe' }, { investigationId: 'test', status: ['completed'] }]) {
    assert.throws(() => parseSearchAccepted(value))
  }
  assert.throws(() => parseInvestigation({ investigationId: 'test', status: 'completed' }))
  assert.throws(() => parseInvestigation({ investigationId: 'test', status: 'completed', candidates: {} }))
  assert.deepEqual(parseInvestigation({ investigationId: 'test', status: 'completed', candidates: [] }).candidates, [])
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
