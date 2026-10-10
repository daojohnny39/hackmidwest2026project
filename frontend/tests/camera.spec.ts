import { readFile } from 'node:fs/promises'
import { test, expect, type Page } from '@playwright/test'

async function startCamera(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'Start camera' }).click()
  await expect(page.getByRole('button', { name: 'Stop camera' })).toBeVisible()
  await page.waitForFunction(() => (document.querySelector('video')?.videoWidth ?? 0) > 0)
}

test('captured photos are analyzed locally, matched to results, and removable', async ({ page }) => {
  const requests: { url: string; method: string }[] = []
  const errors: string[] = []
  page.on('request', (request) => requests.push({ url: request.url(), method: request.method() }))
  page.on('pageerror', (error) => errors.push(error.message))
  await startCamera(page)
  await page.getByRole('button', { name: 'Take photo' }).click()
  const cards = page.locator('.snapshots > li')
  await expect(cards.first().getByText('0 faces detected', { exact: true })).toBeVisible({ timeout: 60_000 })
  const firstId = await cards.first().getAttribute('data-frame-id')
  await cards.first().getByRole('button', { name: 'Show original' }).click()
  await expect(cards.first().getByRole('img')).not.toHaveAttribute('alt', /with facial landmarks/)
  await cards.first().getByRole('button', { name: 'Show landmarks' }).click()
  await expect(cards.first().getByRole('img')).toHaveAttribute('alt', /with facial landmarks/)

  await page.getByRole('button', { name: 'Take photo' }).click()
  await expect(cards).toHaveCount(2)
  await expect(cards.first().getByText('0 faces detected', { exact: true })).toBeVisible()
  const secondId = await cards.first().getAttribute('data-frame-id')
  expect(secondId).not.toBe(firstId)
  const downloadEvent = page.waitForEvent('download')
  await cards.first().getByRole('link', { name: 'Download analysis', exact: true }).click()
  const stream = await (await downloadEvent).createReadStream()
  const chunks: Buffer[] = []
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk))
  const report = JSON.parse(Buffer.concat(chunks).toString())
  expect(report.frame_id).toBe(secondId)
  expect(report.face_count).toBe(0)
  expect(report.image_width).toBeGreaterThan(0)
  expect(report.face_landmarks).toEqual([])
  expect(report.facial_transformation_matrixes).toEqual([])
  expect(requests.filter((r) => r.url.endsWith('/face_landmarker.task'))).toHaveLength(1)
  expect(requests.every((r) => r.method === 'GET')).toBe(true)
  expect(requests.every((r) => r.url.startsWith('http://127.0.0.1:4173/') || r.url.startsWith('blob:'))).toBe(true)
  expect(errors).toEqual([])
  await cards.first().getByRole('button', { name: 'Delete', exact: true }).click()
  await expect(cards).toHaveCount(1)
  await page.getByRole('button', { name: 'Stop camera' }).click()
  await expect(page.getByRole('button', { name: 'Take photo' })).toBeDisabled()
})

test('a failed model load preserves the photo and can be retried', async ({ page }) => {
  await page.route('**/face_landmarker.task', (route) => route.abort())
  await startCamera(page)
  await page.getByRole('button', { name: 'Take photo' }).click()
  await expect(page.getByRole('button', { name: 'Retry analysis' })).toBeVisible({ timeout: 60_000 })
  await expect(page.getByRole('link', { name: 'Download photo', exact: true })).toBeVisible()
  await page.unroute('**/face_landmarker.task')
  await page.getByRole('button', { name: 'Retry analysis' }).click()
  await expect(page.getByText('0 faces detected', { exact: true })).toBeVisible({ timeout: 60_000 })
  await expect(page.locator('.snapshots > li')).toHaveCount(1)
})

test('deleting a photo during loading does not restore it when analysis finishes', async ({ page }) => {
  let releaseModel!: () => void
  const held = new Promise<void>((resolve) => { releaseModel = resolve })
  let requested!: () => void
  const modelRequested = new Promise<void>((resolve) => { requested = resolve })
  await page.route('**/face_landmarker.task', async (route) => {
    requested()
    await held
    await route.continue()
  })
  await startCamera(page)
  await page.getByRole('button', { name: 'Take photo' }).click()
  await modelRequested
  await page.getByRole('button', { name: 'Delete', exact: true }).click()
  await expect(page.locator('.snapshots > li')).toHaveCount(0)
  releaseModel()
  await expect(page.getByRole('button', { name: 'Take photo' })).toBeEnabled({ timeout: 60_000 })
  await expect(page.locator('.snapshots > li')).toHaveCount(0)
  await page.getByRole('button', { name: 'Take photo' }).click()
  await expect(page.getByText('0 faces detected', { exact: true })).toBeVisible()
})

test('analysis works without ImageBitmap and OffscreenCanvas', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'createImageBitmap', { value: undefined, configurable: true })
    Object.defineProperty(window, 'OffscreenCanvas', { value: undefined, configurable: true })
  })
  await startCamera(page)
  await page.getByRole('button', { name: 'Take photo' }).click()
  await expect(page.getByText('0 faces detected', { exact: true })).toBeVisible({ timeout: 60_000 })
  await expect(page.getByRole('link', { name: 'Download landmarks', exact: true })).toBeVisible()
})

test('camera still works when device enumeration is restricted', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.addInitScript(() => {
    navigator.mediaDevices.enumerateDevices = async () => { throw new DOMException('Restricted', 'NotAllowedError') }
  })
  await startCamera(page)
  await page.getByRole('button', { name: 'Take photo' }).click()
  await expect(page.getByText('0 faces detected', { exact: true })).toBeVisible({ timeout: 60_000 })
  expect(errors).toEqual([])
})

test('permission denial is recoverable', async ({ page }) => {
  await page.addInitScript(() => {
    const getUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
    let deny = true
    navigator.mediaDevices.getUserMedia = async (constraints) => {
      if (deny) {
        deny = false
        throw new DOMException('Denied', 'NotAllowedError')
      }
      return getUserMedia(constraints)
    }
  })
  await page.goto('/')
  await page.getByRole('button', { name: 'Start camera' }).click()
  await expect(page.getByText(/Camera permission was denied/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Take photo' })).toBeDisabled()
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByRole('button', { name: 'Take photo' })).toBeEnabled()
})

test('failed playback releases the stream before retrying', async ({ page }) => {
  await page.addInitScript(() => {
    const play = HTMLMediaElement.prototype.play
    let fail = true
    HTMLMediaElement.prototype.play = function () {
      if (fail) {
        fail = false
        const stream = this.srcObject as MediaStream
        Object.assign(window, { failedCameraTracks: stream.getTracks() })
        return Promise.reject(new Error('Preview playback failed.'))
      }
      return play.call(this)
    }
  })
  await page.goto('/')
  await page.getByRole('button', { name: 'Start camera' }).click()
  await expect(page.getByText('Preview playback failed.', { exact: true })).toBeVisible()
  expect(await page.evaluate(() => (window as unknown as { failedCameraTracks: MediaStreamTrack[] })
    .failedCameraTracks.every((track) => track.readyState === 'ended'))).toBe(true)
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByRole('button', { name: 'Take photo' })).toBeEnabled()
})

test('optional personal-photo checkpoint through a simulated camera', async ({ page }, testInfo) => {
  const photoPath = process.env.CHECKPOINT_PHOTO
  test.skip(!photoPath, 'Set CHECKPOINT_PHOTO to a consenting participant’s local JPEG.')
  await page.route('**/__checkpoint_photo.jpeg', async (route) => {
    await route.fulfill({ body: await readFile(photoPath!), contentType: 'image/jpeg' })
  })
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => {
      const image = new Image()
      image.src = '/__checkpoint_photo.jpeg'
      await image.decode()
      const canvas = document.createElement('canvas')
      canvas.width = 756
      canvas.height = Math.round(756 * image.naturalHeight / image.naturalWidth)
      const context = canvas.getContext('2d')!
      const stream = canvas.captureStream(5)
      const draw = () => context.drawImage(image, 0, 0, canvas.width, canvas.height)
      draw()
      const timer = setInterval(draw, 200)
      const track = stream.getVideoTracks()[0]
      const stop = track.stop.bind(track)
      track.stop = () => { clearInterval(timer); stop() }
      return stream
    }
  })
  await startCamera(page)
  await page.getByRole('button', { name: 'Take photo' }).click()
  await expect(page.getByText('1 face detected', { exact: true })).toBeVisible({ timeout: 60_000 })
  await expect(page.getByText('478 landmarks', { exact: false })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('photo-checkpoint.png'), fullPage: true })
})
