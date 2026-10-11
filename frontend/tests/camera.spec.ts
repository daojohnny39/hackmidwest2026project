import { readFile } from 'node:fs/promises'
import { test, expect, type Page } from '@playwright/test'

async function consentToCamera(page: Page, button = 'Open camera') {
  await page.getByRole('button', { name: button, exact: true }).click()
  const accept = page.getByRole('button', { name: 'Agree & open camera', exact: true })
  await expect(accept).toBeDisabled()
  await page.getByRole('dialog').getByRole('checkbox').check()
  await accept.click()
}

async function startCamera(page: Page) {
  await page.goto('/?capture=manual')
  await consentToCamera(page)
  await expect(page.getByRole('button', { name: 'Stop camera', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Take photo', exact: true })).toBeEnabled()
}

const photos = (page: Page) => page.locator('.photo-slot.filled')
const review = (page: Page) => page.locator('.photo-review')
const zeroFaces = (page: Page) => page.getByRole('heading', { name: '0 faces detected', exact: true })

async function takeBlankPhoto(page: Page) {
  await page.getByRole('button', { name: 'Take photo', exact: true }).click()
  await expect(zeroFaces(page)).toBeVisible({ timeout: 60_000 })
}

test('consent precedes capture; local results stay associated with their photo', async ({ page }) => {
  const requests: { url: string; method: string }[] = []
  const errors: string[] = []
  page.on('request', (request) => requests.push({ url: request.url(), method: request.method() }))
  page.on('pageerror', (error) => errors.push(error.message))
  await startCamera(page)
  await takeBlankPhoto(page)
  const firstId = await review(page).getAttribute('data-frame-id')
  await page.getByRole('button', { name: 'Show original', exact: true }).click()
  await expect(review(page).getByRole('img')).not.toHaveAttribute('alt', /with facial landmarks/)
  await page.getByRole('button', { name: 'Show landmarks', exact: true }).click()
  await expect(review(page).getByRole('img')).toHaveAttribute('alt', /with facial landmarks/)

  await consentToCamera(page, 'Retake photo')
  await expect(page.getByRole('button', { name: 'Take photo', exact: true })).toBeEnabled()
  await takeBlankPhoto(page)
  await expect(photos(page)).toHaveCount(1)
  expect(await review(page).getAttribute('data-frame-id')).not.toBe(firstId)
  expect(requests.filter((r) => r.url.endsWith('/face_landmarker.task'))).toHaveLength(1)
  expect(requests.every((r) => r.method === 'GET')).toBe(true)
  expect(requests.every((r) => r.url.startsWith('http://127.0.0.1:4173/') || r.url.startsWith('blob:'))).toBe(true)
  expect(errors).toEqual([])
  expect(await page.locator('video').evaluate(video => (video as HTMLVideoElement).srcObject)).toBeNull()
  await expect(page.getByRole('button', { name: 'Take photo', exact: true })).toHaveCount(0)
  await expect(photos(page)).toHaveCount(1)
  await page.getByRole('button', { name: 'Delete photo 1', exact: true }).click()
  await expect(photos(page)).toHaveCount(0)
  await page.getByRole('button', { name: 'Clear session', exact: true }).click()
  await expect(photos(page)).toHaveCount(0)
  await expect(review(page)).toHaveCount(0)
})

test('a failed model load preserves the photo and can be retried', async ({ page }) => {
  await page.route('**/face_landmarker.task', (route) => route.abort())
  await startCamera(page)
  await page.getByRole('button', { name: 'Take photo', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Retry analysis', exact: true })).toBeVisible({ timeout: 60_000 })
  await expect(photos(page)).toHaveCount(1)
  await page.unroute('**/face_landmarker.task')
  await page.getByRole('button', { name: 'Retry analysis', exact: true }).click()
  await expect(zeroFaces(page)).toBeVisible({ timeout: 60_000 })
  await expect(photos(page)).toHaveCount(1)
})

for (const action of ['Delete photo 1', 'Clear session'] as const) {
  test(`${action} during model loading never restores a discarded photo`, async ({ page }) => {
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
    await page.getByRole('button', { name: 'Take photo', exact: true }).click()
    await modelRequested
    await page.getByRole('button', { name: action, exact: true }).click()
    releaseModel()
    await expect(photos(page)).toHaveCount(0)
    await expect(review(page)).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Open camera', exact: true })).toBeEnabled({ timeout: 60_000 })
    await consentToCamera(page)
    await expect(page.getByRole('button', { name: 'Take photo', exact: true })).toBeEnabled({ timeout: 60_000 })
    await takeBlankPhoto(page)
    await expect(photos(page)).toHaveCount(1)
  })
}

test('one manual capture stops the camera and enables a preview without a name; navigation clears it', async ({ page }) => {
  await startCamera(page)
  await takeBlankPhoto(page)
  await expect(photos(page)).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Retake photo', exact: true })).toBeEnabled()
  await expect(page.getByRole('button', { name: 'Stop camera', exact: true })).toHaveCount(0)
  await page.getByRole('checkbox', { name: /I am this person/ }).check()
  await expect(page.getByRole('textbox', { name: 'First Name optional', exact: true })).toHaveValue('')
  await page.getByRole('button', { name: 'Submit' }).click()
  await expect(page.locator('.candidate-card')).toHaveCount(4)
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'About', exact: true }).click()
  await page.getByRole('link', { name: 'Home', exact: true }).click()
  await expect(photos(page)).toHaveCount(0)
  await expect(page.getByRole('checkbox', { name: 'Automatic capture', exact: true })).not.toBeChecked()
  await expect(page.getByRole('button', { name: 'Open camera', exact: true })).toBeEnabled()
})

test('analysis works without ImageBitmap and OffscreenCanvas', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'createImageBitmap', { value: undefined, configurable: true })
    Object.defineProperty(window, 'OffscreenCanvas', { value: undefined, configurable: true })
  })
  await startCamera(page)
  await takeBlankPhoto(page)
  await expect(page.getByRole('button', { name: 'Show original', exact: true })).toBeVisible()
})

test('camera works when device enumeration is restricted', async ({ page }) => {
  await page.addInitScript(() => {
    navigator.mediaDevices.enumerateDevices = async () => { throw new DOMException('Restricted', 'NotAllowedError') }
  })
  await startCamera(page)
  await takeBlankPhoto(page)
})

test('permission denial is recoverable', async ({ page }) => {
  await page.addInitScript(() => {
    const getUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
    let deny = true
    navigator.mediaDevices.getUserMedia = async (constraints) => {
      if (deny) { deny = false; throw new DOMException('Denied', 'NotAllowedError') }
      return getUserMedia(constraints)
    }
  })
  await page.goto('/?capture=manual')
  await consentToCamera(page)
  await expect(page.getByText(/Camera permission was denied/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Take photo', exact: true })).toHaveCount(0)
  await consentToCamera(page, 'Retry camera')
  await expect(page.getByRole('button', { name: 'Take photo', exact: true })).toBeEnabled()
})

test('failed playback releases the stream before retrying', async ({ page }) => {
  await page.addInitScript(() => {
    const play = HTMLMediaElement.prototype.play
    let fail = true
    HTMLMediaElement.prototype.play = function () {
      if (fail) {
        fail = false
        Object.assign(window, { failedCameraTracks: (this.srcObject as MediaStream).getTracks() })
        return Promise.reject(new Error('Preview playback failed.'))
      }
      return play.call(this)
    }
  })
  await page.goto('/?capture=manual')
  await consentToCamera(page)
  await expect(page.getByText('Preview playback failed.', { exact: true })).toBeVisible()
  expect(await page.evaluate(() => (window as unknown as { failedCameraTracks: MediaStreamTrack[] })
    .failedCameraTracks.every((track) => track.readyState === 'ended'))).toBe(true)
  await consentToCamera(page, 'Retry camera')
  await expect(page.getByRole('button', { name: 'Take photo', exact: true })).toBeEnabled()
})

test('optional photo checkpoint through a simulated camera', async ({ page }, testInfo) => {
  const photoPath = process.env.CHECKPOINT_PHOTO
  test.skip(!photoPath, 'Set CHECKPOINT_PHOTO to an approved local test JPEG.')
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
      const draw = () => context.drawImage(image, 0, 0, canvas.width, canvas.height)
      draw()
      const stream = canvas.captureStream(5)
      const timer = setInterval(draw, 200)
      const track = stream.getVideoTracks()[0]
      const stop = track.stop.bind(track)
      track.stop = () => { clearInterval(timer); stop() }
      return stream
    }
  })
  await startCamera(page)
  await page.getByRole('button', { name: 'Take photo', exact: true }).click()
  await expect(page.getByRole('heading', { name: '1 face detected', exact: true })).toBeVisible({ timeout: 60_000 })
  await expect(page.getByText('478 landmarks', { exact: false })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('photo-checkpoint.png'), fullPage: true })
})
