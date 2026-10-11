import { test, expect, type Page } from '@playwright/test'
import type { TestVision } from './vision'

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    window.testVision = { mode: 'no_face', calls: 0, closed: 0 }
    navigator.mediaDevices.getUserMedia = async () => {
      const canvas = document.createElement('canvas')
      canvas.width = 1280; canvas.height = 720
      const ctx = canvas.getContext('2d')!
      const draw = () => {
        ctx.fillStyle = (window as unknown as { testCameraColor?: string }).testCameraColor ?? '#ff0000'; ctx.fillRect(0, 0, 640, 720)
        ctx.fillStyle = '#0000ff'; ctx.fillRect(640, 0, 640, 720)
      }
      draw()
      const stream = canvas.captureStream(30)
      const timer = setInterval(draw, 33)
      const track = stream.getVideoTracks()[0]
      const stop = track.stop.bind(track)
      track.stop = () => { clearInterval(timer); stop() }
      Object.assign(window, { testTracks: stream.getTracks() })
      return stream
    }
    navigator.mediaDevices.enumerateDevices = async () => []
  })
})
async function openCamera(page: Page, button = 'Start camera') {
  await page.getByRole('button', { name: button, exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'Camera consent' })).toBeVisible()
  expect(await page.evaluate(() => Boolean(document.querySelector('video')?.srcObject))).toBe(false)
  await expect(page.getByRole('button', { name: 'Agree & open camera' })).toBeDisabled()
  await page.getByRole('dialog').getByRole('checkbox').check()
  await page.getByRole('button', { name: 'Agree & open camera' }).click()
}

async function captured(page: Page) {
  await expect(page.getByRole('article', { name: 'Captured front photo' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Submit' })).toBeEnabled()
}

async function metadata(page: Page) {
  const pending = page.waitForEvent('download')
  await page.getByRole('link', { name: 'Download capture metadata' }).click()
  const stream = await (await pending).createReadStream()
  const chunks: Buffer[] = []
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk))
  return JSON.parse(Buffer.concat(chunks).toString())
}

async function setup(page: Page, mode: TestVision['mode'] = 'valid') {
  await page.goto('/')
  await page.evaluate(value => { window.testVision.mode = value }, mode)
  await expect(page.getByRole('checkbox', { name: 'Automatic capture', exact: true })).toBeChecked()
  await expect(page.getByRole('button', { name: 'Take photo', exact: true })).toHaveCount(0)
  await page.getByRole('checkbox', { name: /I am this person/ }).check()
  await expect(page.getByRole('button', { name: 'Submit' })).toBeDisabled()
}

test('default automatic capture supplies the subject reference after consent and qualifying hold', async ({ page }, testInfo) => {
  const uploads: string[] = []
  page.on('request', request => { if (request.method() !== 'GET') uploads.push(request.url()) })
  await setup(page)
  await openCamera(page)
  await captured(page)
  await expect(page.locator('.auto-stage')).toBeHidden()
  const report = await metadata(page)
  expect(report.qualification.faceCount).toBe(1)
  expect(report.qualification.framingPassed).toBe(true)
  expect(report.qualification.holdMs).toBeGreaterThanOrEqual(400)
  expect(report.toleranceDegrees).toEqual({ yaw: 5, pitch: 5, roll: 5 })
  expect(await page.evaluate(() => (window as unknown as { testTracks: MediaStreamTrack[] }).testTracks.every(track => track.readyState === 'ended'))).toBe(true)
  await page.getByRole('button', { name: 'Submit' }).click()
  await expect(page.locator('.candidate-card')).toHaveCount(4)
  expect(uploads).toEqual([])
  for (const width of [1280, 390, 320]) {
    await page.setViewportSize({ width, height: 844 })
    await page.getByLabel('About subject details').click()
    await expect(page.getByText('Providing information in the fields below helps narrow results. All fields are optional.')).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    await page.getByLabel('About subject details').click()
  }
  await page.screenshot({ path: testInfo.outputPath('automatic-studio.png'), fullPage: true })
  await page.getByRole('button', { name: 'Delete photo', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Submit' })).toBeDisabled()
  await expect(page.locator('.candidate-card')).toHaveCount(0)
})

test('retake preserves the reference on failure and replaces it after success', async ({ page }) => {
  await setup(page)
  await openCamera(page)
  await captured(page)
  const first = await metadata(page)
  await page.evaluate(() => { window.testVision.failure = true })
  await openCamera(page, 'Retake front photo')
  await expect(page.getByText(/Face analysis could not load/)).toBeVisible()
  expect((await metadata(page)).captureId).toBe(first.captureId)
  await expect(page.getByRole('button', { name: 'Submit' })).toBeEnabled()
  await page.evaluate(() => { window.testVision.failure = false })
  await openCamera(page, 'Try again')
  await expect(page.getByRole('status')).toHaveText('Front photo captured.')
  await captured(page)
  expect((await metadata(page)).captureId).not.toBe(first.captureId)
})

test('clear session cancels a pending capture and supports a fresh automatic session', async ({ page }) => {
  await page.addInitScript(() => {
    const encode = HTMLCanvasElement.prototype.toBlob
    HTMLCanvasElement.prototype.toBlob = function (...args) { setTimeout(() => encode.apply(this, args), 1200) }
  })
  await setup(page)
  await openCamera(page)
  await expect(page.getByText('Saving your front photo…', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Clear session', exact: true }).click()
  await page.waitForTimeout(1400)
  await expect(page.getByRole('article', { name: 'Captured front photo' })).toHaveCount(0)
  await expect(page.getByRole('textbox', { name: 'First Name optional', exact: true })).toHaveValue('')
  await expect(page.getByRole('button', { name: 'Submit' })).toBeDisabled()
  await page.getByRole('checkbox', { name: /I am this person/ }).check()
  await openCamera(page)
  await captured(page)
})

test('manual selection and navigation release the automatic session', async ({ page }) => {
  await setup(page, 'no_face')
  await openCamera(page)
  await expect(page.getByText('Move your face into view.', { exact: true })).toBeVisible()
  await page.getByRole('checkbox', { name: 'Mirror preview', exact: true }).uncheck()
  await page.getByRole('checkbox', { name: 'Automatic capture', exact: true }).uncheck()
  expect(await page.evaluate(() => (window as unknown as { testTracks: MediaStreamTrack[] }).testTracks.every(track => track.readyState === 'ended'))).toBe(true)
  await expect(page.getByRole('button', { name: 'Open camera', exact: true })).toBeVisible()
  await expect(page.getByRole('checkbox', { name: 'Mirror preview', exact: true })).not.toBeChecked()
  await expect(page.locator('video')).toHaveCSS('transform', 'none')
  await page.getByRole('checkbox', { name: 'Automatic capture', exact: true }).check()
  await expect(page.getByRole('checkbox', { name: 'Mirror preview', exact: true })).not.toBeChecked()
  await page.evaluate(() => { window.testVision.mode = 'valid' })
  await openCamera(page)
  await expect(page.getByRole('article', { name: 'Captured front photo' })).toBeVisible()
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'About', exact: true }).click()
  await page.getByRole('link', { name: 'Home', exact: true }).click()
  await expect(page.getByRole('checkbox', { name: 'Automatic capture', exact: true })).toBeChecked()
  await expect(page.getByRole('article', { name: 'Captured front photo' })).toHaveCount(0)
})
