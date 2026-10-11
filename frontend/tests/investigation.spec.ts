import { test, expect } from '@playwright/test'
import { fakeSearchApi, hasField, INVESTIGATION_ID, MATCH } from './searchApi'

test('optional subject details explain narrowing results; a photo and authorization are still needed', async ({ page }) => {
  await page.goto('/')
  const name = page.getByRole('textbox', { name: 'First Name optional', exact: true })
  await name.fill(' ')
  await page.getByLabel('Last Name optional', { exact: true }).focus()
  for (const label of ['First Name', 'Last Name', 'City', 'Age']) await expect(page.getByLabel(`${label} optional`, { exact: true })).not.toHaveAttribute('required', '')
  await expect(page.locator('.subject-form label .field-hint')).toHaveText(['optional', 'optional', 'optional', 'optional'])
  await expect(page.getByText('Enter the subject’s name.')).toHaveCount(0)
  await page.getByLabel('About subject details').click()
  await expect(page.getByText('Providing information in the fields below helps narrow results. All fields are optional.')).toBeVisible()
  await name.fill('Sample')
  await page.getByLabel('Last Name optional', { exact: true }).fill('Person')
  await page.getByLabel('City optional', { exact: true }).fill('Kansas City')
  await page.getByLabel('Age optional', { exact: true }).fill('32')
  await page.getByRole('checkbox', { name: /I am this person/ }).check()
  await page.getByRole('checkbox', { name: /Send my face to the search services/ }).check()
  await expect(page.getByRole('button', { name: 'Search for my photo' })).toBeDisabled()
  await page.getByRole('button', { name: 'Clear session', exact: true }).click()
  await expect(name).toHaveValue('')
  for (const label of ['Last Name', 'City', 'Age']) await expect(page.getByLabel(`${label} optional`, { exact: true })).toHaveValue('')
  await expect(page.getByRole('checkbox', { name: /I am this person/ })).not.toBeChecked()
  await expect(page.getByRole('checkbox', { name: /Send my face to the search services/ })).not.toBeChecked()
})

test('sample cards are source-linked evidence with adjacent confidence and risk badges', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'View sample', exact: true }).click()
  await expect(page.locator('.candidate-card')).toHaveCount(4)
  await expect(page.getByText('Confidence reflects contextual evidence and does not verify a person’s identity.')).toBeVisible()
  await expect(page.getByText('Confidence: unknown', { exact: true })).toBeVisible()
  const firstCard = page.locator('.candidate-card').first()
  await expect(firstCard.locator('.candidate-labels .evidence-badge')).toHaveText(['Confidence: high', 'Threat: not assessed'])
  await expect(firstCard.locator('.candidate-bottom')).not.toContainText('Threat:')
  const link = page.getByRole('link', { name: 'Open source: Sample professional profile' })
  await expect(link).toHaveAttribute('href', 'https://example.com/profile')
  await expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  await page.getByLabel('Sample scenario').selectOption('missing')
  await expect(page.locator('.candidate-card')).toHaveCount(2)
  await expect(page.getByText('Image unavailable', { exact: true })).toHaveCount(2)
  await expect(page.getByText('No source link', { exact: true })).toBeVisible()
})

test('failed thumbnails fall back without losing the source', async ({ page }) => {
  await page.route('**/demo/source-placeholder.svg', route => route.abort())
  await page.goto('/')
  await page.getByRole('button', { name: 'View sample', exact: true }).click()
  await page.getByLabel('Sample scenario').selectOption('one')
  await expect(page.getByText('Image unavailable', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Open source: Sample professional profile' })).toBeVisible()
})

test('empty, failed, partial, and pending states stay distinct', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'View sample', exact: true }).click()
  const select = page.getByLabel('Sample scenario')
  await select.selectOption('empty')
  await expect(page.getByRole('heading', { name: 'No findings', exact: true })).toBeVisible()
  await expect(page.locator('.candidate-card')).toHaveCount(0)
  await select.selectOption('failed')
  await expect(page.getByRole('heading', { name: 'Search failed' })).toBeVisible()
  await page.getByRole('button', { name: 'Retry sample' }).click()
  await expect(page.locator('.candidate-card')).toHaveCount(4)
  await select.selectOption('partial')
  await expect(page.getByText('Some sources are unavailable. These findings are incomplete.')).toBeVisible()
  await expect(page.locator('.candidate-card')).toHaveCount(2)
  await select.selectOption('queued')
  await expect(page.getByText('Sample state only. No search is running.')).toBeVisible()
  await select.selectOption('running')
  await expect(page.getByRole('heading', { name: 'Review in progress' })).toBeVisible()
})

test('captured reference starts a live search even if optional landmark analysis fails', async ({ page }) => {
  await page.route('**/face_landmarker.task', route => route.abort())
  const uploads = await fakeSearchApi(page)
  await page.goto('/?capture=manual')
  await page.getByRole('button', { name: 'Open camera', exact: true }).click()
  await page.getByRole('dialog').getByRole('checkbox').check()
  await page.getByRole('button', { name: 'Agree & open camera', exact: true }).click()
  await page.getByRole('button', { name: 'Take photo', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Retry analysis', exact: true })).toBeVisible()
  await page.getByRole('checkbox', { name: /I am this person/ }).check()
  const search = page.getByRole('button', { name: 'Search for my photo' })
  await expect(search).toBeDisabled()
  await page.getByRole('checkbox', { name: /Send my face to the search services/ }).check()
  await page.getByLabel('First Name optional', { exact: true }).fill(' Sample ')
  await page.getByLabel('Age optional', { exact: true }).fill('32')
  await search.click()
  await expect(page.getByRole('heading', { name: '1 matching source', exact: true })).toBeVisible()
  await expect(page.getByText('Confidence: 97% · likely match', { exact: true })).toBeVisible()
  await expect(page.getByText('Face similarity 97/100 from Amazon Rekognition (likely match).')).toBeVisible()
  await expect(page.getByRole('link', { name: 'Open source: Impostor profile' })).toHaveAttribute('href', 'https://example.com/impostor')
  expect(uploads).toHaveLength(1)
  expect(hasField(uploads[0], 'authorized', 'true') && hasField(uploads[0], 'externalProcessing', 'true')).toBe(true)
  expect(uploads[0]).toContain('filename="reference.jpg"')
  // Given details are sent trimmed; empty ones are left out and the server stores them as empty.
  expect(hasField(uploads[0], 'firstName', 'Sample') && hasField(uploads[0], 'age', '32')).toBe(true)
  expect(uploads[0]).not.toContain('name="lastName"')
  await page.getByRole('button', { name: 'Delete photo 1', exact: true }).click()
  await expect(search).toBeDisabled()
  await expect(page.locator('.candidate-card')).toHaveCount(0)
})

test('an unreachable service and a failed search are errors, not empty results', async ({ page }) => {
  await page.route('**/api/search', route => route.fulfill({ status: 502, body: 'Bad gateway' }))
  await page.goto('/?capture=manual')
  await page.getByRole('button', { name: 'Open camera', exact: true }).click()
  await page.getByRole('dialog').getByRole('checkbox').check()
  await page.getByRole('button', { name: 'Agree & open camera', exact: true }).click()
  await page.getByRole('button', { name: 'Take photo', exact: true }).click()
  await page.getByRole('checkbox', { name: /I am this person/ }).check()
  await page.getByRole('checkbox', { name: /Send my face to the search services/ }).check()
  await page.getByRole('button', { name: 'Search for my photo' }).click()
  await expect(page.getByRole('alert')).toHaveText('The search service is not reachable. Check that it is running.')
  await expect(page.getByRole('heading', { name: 'No matching sources' })).toHaveCount(0)

  await page.unroute('**/api/search')
  await page.route('**/api/search', route => route.fulfill({ status: 202, json: { investigationId: 'inv_0123456789abcdef', status: 'queued' } }))
  await page.route('**/api/search/inv_0123456789abcdef', route => route.fulfill({ json: { investigationId: 'inv_0123456789abcdef', status: 'failed', error: 'search_failed' } }))
  await page.getByRole('button', { name: 'Search for my photo' }).click()
  await expect(page.getByRole('heading', { name: 'Search failed' })).toBeVisible()
  await expect(page.getByText('The search could not finish. Try again.')).toBeVisible()
})

test('live search displays the current server stage until findings arrive', async ({ page }) => {
  await page.route('**/face_landmarker.task', route => route.abort())
  let status = 'queued'
  let stage: string | undefined
  await page.route('**/api/search', route => route.fulfill({ status: 202, json: { investigationId: INVESTIGATION_ID, status: 'queued' } }))
  await page.route(`**/api/search/${INVESTIGATION_ID}`, route => route.fulfill({
    json: { investigationId: INVESTIGATION_ID, status, stage, candidates: status === 'completed' ? [MATCH] : undefined },
  }))
  await page.goto('/?capture=manual')
  await page.getByRole('button', { name: 'Open camera', exact: true }).click()
  await page.getByRole('dialog').getByRole('checkbox').check()
  await page.getByRole('button', { name: 'Agree & open camera', exact: true }).click()
  await page.getByRole('button', { name: 'Take photo', exact: true }).click()
  await page.getByRole('checkbox', { name: /I am this person/ }).check()
  await page.getByRole('checkbox', { name: /Send my face to the search services/ }).check()
  await page.getByRole('button', { name: 'Search for my photo' }).click()
  const panel = page.getByRole('complementary', { name: 'Search results' })
  await expect(panel.getByRole('heading', { name: 'Queued', exact: true })).toBeVisible()
  await expect(panel.getByText('Waiting for an available search slot.')).toBeVisible()
  await expect(panel.locator('.search-loading')).toBeVisible()
  status = 'running'
  for (const [code, heading] of [
    ['preparing', 'Preparing your photo'],
    ['searching', 'Searching public images'],
    ['comparing', 'Comparing faces'],
    ['reviewing', 'Reviewing sources'],
    ['checking', 'Checking source safety'],
    ['finalizing', 'Preparing findings'],
    ['unfamiliar-stage', 'Searching'],
  ]) {
    stage = code
    await expect(panel.getByRole('heading', { name: heading, exact: true })).toBeVisible()
    await expect(panel.locator('.search-loading')).toBeVisible()
  }
  status = 'completed'
  await expect(panel.getByRole('heading', { name: '1 matching source', exact: true })).toBeVisible()
  await expect(panel.locator('.search-loading')).toHaveCount(0)
})

test('threat levels show on each source, with a summary of flagged sources', async ({ page }) => {
  await page.route('**/face_landmarker.task', route => route.abort())
  await fakeSearchApi(page, [
    { ...MATCH, threat: { score: 100, level: 'high',
      indicators: ['Dev mode marks evilassociation.hakr as a malicious fraud site.'] } },
    { ...MATCH, id: 'src-2', sourceUrl: 'https://www.instagram.com/p/abc/', pageTitle: 'Instagram post',
      threat: { score: 0, level: 'low', indicators: ['Dev mode marks every site except evilassociation.hakr as benign.'] } },
    { ...MATCH, id: 'src-3', sourceUrl: 'https://example.org/unchecked', pageTitle: 'Unchecked page' },
  ])
  await page.goto('/?capture=manual')
  await page.getByRole('button', { name: 'Open camera', exact: true }).click()
  await page.getByRole('dialog').getByRole('checkbox').check()
  await page.getByRole('button', { name: 'Agree & open camera', exact: true }).click()
  await page.getByRole('button', { name: 'Take photo', exact: true }).click()
  await page.getByRole('checkbox', { name: /I am this person/ }).check()
  await page.getByRole('checkbox', { name: /Send my face to the search services/ }).check()
  await page.getByRole('button', { name: 'Search for my photo' }).click()
  await expect(page.getByRole('heading', { name: '3 matching sources', exact: true })).toBeVisible()
  await expect(page.getByText('1 of these sources is flagged as a threat. Open them with care.')).toBeVisible()
  const flagged = page.getByRole('article', { name: 'Impostor profile' })
  await expect(flagged.getByText('Threat: 100% · high', { exact: true })).toBeVisible()
  await flagged.getByText('Threat details').click()
  await expect(flagged.getByText('Dev mode marks evilassociation.hakr as a malicious fraud site.')).toBeVisible()
  await expect(page.getByRole('article', { name: 'Instagram post' }).getByText('Threat: 0% · low', { exact: true })).toBeVisible()
  await expect(page.getByRole('article', { name: 'Unchecked page' }).getByText('Threat: not assessed', { exact: true })).toBeVisible()
})
