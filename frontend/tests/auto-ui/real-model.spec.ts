import { test, expect } from '@playwright/test'

test('real local VIDEO model rejects the no-face camera and explains prolonged failure', async ({ page }, testInfo) => {
  const requests: { url: string; method: string }[] = []
  const errors: string[] = []
  page.on('request', (r) => requests.push({ url: r.url(), method: r.method() }))
  page.on('pageerror', (e) => errors.push(e.message))
  await page.goto('/?mode=front')
  await expect(page.getByRole('button', { name: 'Take photo' })).toHaveCount(0)
  await page.getByRole('button', { name: 'Start camera' }).click()
  await expect(page.getByRole('status')).toHaveText('Move your face into view.', { timeout: 60_000 })
  await expect(page.getByRole('complementary', { name: 'Capture troubleshooting' })).toBeVisible({ timeout: 20_000 })
  await expect(page.getByRole('article', { name: 'Captured front photo' })).toHaveCount(0)
  await expect(page.getByText('Capture diagnostics · 0 hold resets', { exact: true })).toBeVisible()
  expect(requests.every((r) => r.method === 'GET')).toBe(true)
  expect(requests.every((r) => r.url.startsWith('http://127.0.0.1:4174/') || r.url.startsWith('blob:'))).toBe(true)
  expect(errors).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('front-guidance.png'), fullPage: true })
  await page.getByRole('button', { name: 'Pause capture' }).click()
  await expect(page.getByRole('button', { name: 'Resume capture' })).toBeVisible()
  expect(await page.locator('video').evaluate((v: HTMLVideoElement) => v.srcObject)).toBeNull()
  await page.getByRole('button', { name: 'Resume capture' }).click()
  await expect(page.getByRole('status')).toHaveText('Move your face into view.', { timeout: 60_000 })
  await page.getByRole('button', { name: 'Stop camera' }).click()
  await expect(page.getByRole('button', { name: 'Start camera' })).toBeVisible()
})

test('automatic capture recovers from local model loading failure', async ({ page }) => {
  await page.route('**/face_landmarker.task', (route) => route.abort())
  await page.goto('/?mode=front')
  await page.getByRole('button', { name: 'Start camera' }).click()
  await expect(page.getByRole('status')).toContainText('Face analysis could not load', { timeout: 60_000 })
  expect(await page.locator('video').evaluate((v: HTMLVideoElement) => v.srcObject)).toBeNull()
  await page.unroute('**/face_landmarker.task')
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByRole('status')).toHaveText('Move your face into view.', { timeout: 60_000 })
})

test('stop during local model loading cannot rearm capture', async ({ page }) => {
  let release!: () => void, requested!: () => void
  const pending = new Promise<void>((r) => { release = r })
  const request = new Promise<void>((r) => { requested = r })
  await page.route('**/face_landmarker.task', async (route) => { requested(); await pending; await route.continue() })
  await page.goto('/?mode=front')
  await page.getByRole('button', { name: 'Start camera' }).click()
  await request
  await page.getByRole('button', { name: 'Stop camera' }).click()
  release()
  await expect(page.getByRole('button', { name: 'Start camera' })).toBeVisible()
  await expect(page.getByRole('status')).toContainText('Face the camera at eye level')
  expect(await page.locator('video').evaluate((v: HTMLVideoElement) => v.srcObject)).toBeNull()
  await expect(page.getByRole('article', { name: 'Captured front photo' })).toHaveCount(0)
})
