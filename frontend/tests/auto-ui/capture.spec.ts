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
async function start(page: Page, mode: TestVision['mode'] = 'no_face') {
  await page.goto('/?mode=front')
  await expect(page.getByRole('region', { name: 'Front-facing automatic capture' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Take photo' })).toHaveCount(0)
  await page.evaluate((next) => { window.testVision.mode = next }, mode)
  await page.getByRole('button', { name: 'Start camera' }).click()
}
async function jsonDownload(page: Page, name: string) {
  const pending = page.waitForEvent('download')
  await page.getByRole('link', { name, exact: true }).click()
  const stream = await (await pending).createReadStream()
  const chunks: Buffer[] = []
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk))
  return JSON.parse(Buffer.concat(chunks).toString())
}

test('one start captures once, releases camera, and saves full-resolution unmirrored JPEG plus metadata', async ({ page }, testInfo) => {
  await start(page, 'valid')
  await expect(page.getByRole('status')).toHaveText('Front photo captured.')
  await expect(page.getByRole('article', { name: 'Captured front photo' })).toHaveCount(1)
  await expect(page.locator('.auto-stage')).toBeHidden()
  await expect(page.getByRole('button', { name: 'Retake front photo' })).toBeVisible()
  const settings = await page.evaluate(() => window.testVision)
  expect(settings.inferenceWidth).toBe(640); expect(settings.inferenceHeight).toBe(360)
  expect(settings.closed).toBe(1)
  expect(await page.evaluate(() => (window as unknown as { testTracks: MediaStreamTrack[] }).testTracks.every((t) => t.readyState === 'ended'))).toBe(true)
  const metadata = await jsonDownload(page, 'Download capture metadata')
  expect(metadata.view).toBe('front')
  expect(metadata.qualification.holdMs).toBeGreaterThanOrEqual(400)
  expect(metadata.qualification.sampleCount).toBeGreaterThanOrEqual(3)
  expect(metadata.toleranceDegrees).toEqual({ yaw: 5, pitch: 5, roll: 5 })
  expect(metadata.image).toEqual({ mimeType: 'image/jpeg', width: 1280, height: 720 })
  const pixels = await page.getByRole('img', { name: 'Captured front-facing photo, unmirrored' }).evaluate(async (img: HTMLImageElement) => {
    await img.decode()
    const canvas = document.createElement('canvas'); canvas.width = img.naturalWidth; canvas.height = img.naturalHeight
    const ctx = canvas.getContext('2d')!; ctx.drawImage(img, 0, 0)
    return { left: Array.from(ctx.getImageData(10, 10, 1, 1).data), right: Array.from(ctx.getImageData(1270, 10, 1, 1).data) }
  })
  expect(pixels.left[0]).toBeGreaterThan(245); expect(pixels.left[2]).toBeLessThan(10)
  expect(pixels.right[2]).toBeGreaterThan(245)
  await page.screenshot({ path: testInfo.outputPath('auto-captured.png'), fullPage: true })
  expect(await page.evaluate(() => window.testVision.calls)).toBe(settings.calls)
})

test('three interrupted holds show troubleshooting with recorded reasons and no capture', async ({ page }) => {
  await start(page, 'resets')
  await expect(page.getByRole('complementary', { name: 'Capture troubleshooting' })).toBeVisible()
  await page.getByText(/Capture diagnostics ·/).click()
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Download reset log' }).click()
  const stream = await (await download).createReadStream()
  const chunks: Buffer[] = []; for await (const chunk of stream!) chunks.push(Buffer.from(chunk))
  const log = JSON.parse(Buffer.concat(chunks).toString())
  expect(log.failedHoldCount).toBeGreaterThanOrEqual(3)
  expect(log.resetCounts.yaw).toBeGreaterThanOrEqual(3)
  expect(log.recentResets[0].reasons).toContain('yaw')
  expect(log.preset.tolerance.yaw).toBe(5)
  await expect(page.getByRole('article', { name: 'Captured front photo' })).toHaveCount(0)
})

test('retake preserves previous photo on failure and replaces only on success', async ({ page }) => {
  await start(page, 'valid')
  await expect(page.getByRole('status')).toHaveText('Front photo captured.')
  const first = await jsonDownload(page, 'Download capture metadata')
  await page.evaluate(() => { window.testVision.failure = true })
  await page.getByRole('button', { name: 'Retake front photo' }).click()
  await expect(page.getByRole('status')).toContainText('Face analysis could not load')
  expect((await jsonDownload(page, 'Download capture metadata')).captureId).toBe(first.captureId)
  await page.evaluate(() => { window.testVision.failure = false })
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByRole('status')).toHaveText('Front photo captured.')
  const second = await jsonDownload(page, 'Download capture metadata')
  expect(second.captureId).not.toBe(first.captureId)
  await expect(page.getByRole('article', { name: 'Captured front photo' })).toHaveCount(1)
  await page.getByRole('button', { name: 'Delete photo' }).click()
  await expect(page.getByRole('article', { name: 'Captured front photo' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Retake front photo' })).toBeVisible()
})

test('chin guidance, mirroring, pause, resume and stop leave capture disarmed', async ({ page }) => {
  await start(page, 'pitch_up')
  await expect(page.getByRole('status')).toHaveText('Lower your chin slightly.')
  await page.evaluate(() => { window.testVision.mode = 'pitch_down' })
  await expect(page.getByRole('status')).toHaveText('Raise your chin slightly.')
  await page.getByRole('checkbox', { name: 'Mirror preview' }).uncheck()
  await expect(page.locator('video')).not.toHaveClass('mirrored')
  await page.getByRole('button', { name: 'Pause capture' }).click()
  await expect(page.getByRole('button', { name: 'Resume capture' })).toBeVisible()
  await page.getByRole('button', { name: 'Resume capture' }).click()
  await expect(page.getByRole('status')).toHaveText('Raise your chin slightly.')
  await page.getByRole('button', { name: 'Stop camera' }).click()
  await expect(page.getByRole('button', { name: 'Start camera' })).toBeVisible()
  await expect(page.getByRole('article', { name: 'Captured front photo' })).toHaveCount(0)
})

test('stop while JPEG encoding is delayed discards that pending capture', async ({ page }) => {
  await page.addInitScript(() => {
    const encode = HTMLCanvasElement.prototype.toBlob
    HTMLCanvasElement.prototype.toBlob = function (...args) { setTimeout(() => encode.apply(this, args), 1200) }
  })
  await start(page, 'valid')
  await expect(page.getByRole('status')).toHaveText('Saving your front photo…')
  await page.getByRole('button', { name: 'Stop camera' }).click()
  await expect(page.getByRole('button', { name: 'Start camera' })).toBeVisible()
  await page.waitForTimeout(1400) // Deliberately exceed the controlled delayed encoder.
  await expect(page.getByRole('article', { name: 'Captured front photo' })).toHaveCount(0)
})

test('delayed encoding saves the qualifying pixels even as the source camera changes', async ({ page }) => {
  await page.addInitScript(() => {
    const encode = HTMLCanvasElement.prototype.toBlob
    HTMLCanvasElement.prototype.toBlob = function (...args) {
      Object.assign(window, { testCameraColor: '#00ff00' })
      setTimeout(() => encode.apply(this, args), 300)
    }
  })
  await start(page, 'valid')
  await expect(page.getByRole('status')).toHaveText('Front photo captured.')
  const pixel = await page.getByRole('img', { name: 'Captured front-facing photo, unmirrored' }).evaluate(async (img: HTMLImageElement) => {
    await img.decode()
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1
    const ctx = canvas.getContext('2d')!
    ctx.drawImage(img, 10, 10, 1, 1, 0, 0, 1, 1)
    return Array.from(ctx.getImageData(0, 0, 1, 1).data)
  })
  expect(pixel[0]).toBeGreaterThan(245)
  expect(pixel[1]).toBeLessThan(10)
})

test('hiding the page clears the hold and requires explicit resume', async ({ page }) => {
  await start(page)
  await expect(page.getByRole('status')).toHaveText('Move your face into view.')
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(page.getByRole('button', { name: 'Resume capture' })).toBeVisible()
  expect(await page.locator('video').evaluate((v: HTMLVideoElement) => v.srcObject)).toBeNull()
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(page.getByRole('button', { name: 'Resume capture' })).toBeVisible()
  await expect(page.getByRole('article', { name: 'Captured front photo' })).toHaveCount(0)
})

test('camera disconnection during encoding discards the photo and offers retry', async ({ page }) => {
  await page.addInitScript(() => {
    const encode = HTMLCanvasElement.prototype.toBlob
    HTMLCanvasElement.prototype.toBlob = function (...args) { setTimeout(() => encode.apply(this, args), 800) }
  })
  await start(page, 'valid')
  await expect(page.getByRole('status')).toHaveText('Saving your front photo…')
  await page.evaluate(() => {
    const [track] = (window as unknown as { testTracks: MediaStreamTrack[] }).testTracks
    track.dispatchEvent(new Event('ended'))
  })
  await expect(page.getByRole('status')).toHaveText('The camera disconnected.')
  await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible()
  await page.waitForTimeout(1000) // Let the cancelled encoder finish.
  await expect(page.getByRole('article', { name: 'Captured front photo' })).toHaveCount(0)
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByRole('status')).toHaveText('Front photo captured.')
})

test('guidance and camera fit narrow and desktop views without horizontal overflow', async ({ page }, testInfo) => {
  await start(page)
  await expect(page.getByRole('status')).toHaveText('Move your face into view.')
  for (const [width, height] of [[1280, 800], [390, 844]]) {
    await page.setViewportSize({ width, height })
    const box = await page.getByRole('status').boundingBox()
    expect(box!.y + box!.height).toBeLessThan(height)
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    const stage = await page.locator('.auto-stage').boundingBox()
    const oval = await page.locator('.face-guide').boundingBox()
    expect(Math.abs((oval!.x + oval!.width / 2) - (stage!.x + stage!.width / 2))).toBeLessThan(1)
    expect(Math.abs((oval!.y + oval!.height / 2) - (stage!.y + stage!.height / 2))).toBeLessThan(1)
    await page.screenshot({ path: testInfo.outputPath(`front-layout-${width}.png`), fullPage: true })
  }
})
