import type { Page } from '@playwright/test'

export const INVESTIGATION_ID = 'inv_0123456789abcdef'
export const MATCH = {
  id: 'src-1', sourceUrl: 'https://example.com/impostor', pageTitle: 'Impostor profile', sourceType: 'Instagram profile',
  confidence: { score: 97, level: 'likely', reasons: ['Face similarity 97/100 from Amazon Rekognition (likely match).'] },
}

/** Stands in for `python -m pipeline serve`: accepts one search, reports it running once, then completed
 *  with MATCH. Returns the multipart bodies it received, decoded as Latin-1 text. */
export async function fakeSearchApi(page: Page, candidates: object[] = [MATCH]): Promise<string[]> {
  const uploads: string[] = []
  await page.route('**/api/search', async route => {
    uploads.push(route.request().postDataBuffer()?.toString('latin1') ?? '')
    await route.fulfill({ status: 202, json: { investigationId: INVESTIGATION_ID, status: 'queued' } })
  })
  let polls = 0
  await page.route(`**/api/search/${INVESTIGATION_ID}`, route => route.fulfill({
    json: ++polls < 2 ? { investigationId: INVESTIGATION_ID, status: 'running' }
      : { investigationId: INVESTIGATION_ID, status: 'completed', candidates, warnings: [] },
  }))
  return uploads
}

/** Whether a multipart body carries this form field with exactly this value. */
export function hasField(body: string, name: string, value: string) {
  return body.includes(`name="${name}"\r\n\r\n${value}\r\n`)
}
