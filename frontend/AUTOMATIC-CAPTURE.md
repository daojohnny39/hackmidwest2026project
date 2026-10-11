# Preserved capture checkpoints

React + TypeScript + Vite app with front-facing automatic capture at `?mode=front` and
a separate manual camera checkpoint. The camera page consumes `useAutoCapture`;
the same guidance/state contract supports later UI integration.
MediaPipe runs locally; the app does not upload images.

First download `models/face_landmarker.task` in the project root using Step 3 of
the [project README](../README.md). Then run these commands from `frontend/`:

```bash
npm install
npm run dev       # http://localhost:5173
npm run build     # type-check + production build into dist/
npm run lint
```

`npm run dev` and `npm run build` verify the pinned SDK version and model checksum, then copy the model and the installed
MediaPipe WebAssembly files to `public/mediapipe/`. That generated directory is
ignored by Git. Production builds include these assets in `dist/`; no CDN or
Python analysis service is needed at runtime.

## Automatic capture

Open the local Vite URL with `?mode=front`, choose **Start camera**, and allow
camera access. Follow the live instruction and hold steady. The app takes one
photo automatically after every angle and framing check passes for 400 ms, then
stops the camera. No shutter click is needed. Choose **Retake front photo** to
start again. The page stays simple, with no mode selector.

`src/CameraCapture.tsx` attaches `camera.videoRef` to the video and invokes
`useAutoCapture().start()` from the Start action. The hook never starts on mount.

Render `state.guidanceCode` through `guidance(state)`, the hold progress and
framing/pose state, and `troubleshooting(state)` when `state.troubleshooting` is
true. The hook exposes pause/resume/stop/retry/retake/changeCamera actions and the
latest `result`. The component owns Blob URLs for previews and downloads and must
revoke them when replacing/deleting images or unmounting.

The engine still requires **±5° on each axis** and a **400 ms** qualifying hold.
It saves the exact qualifying full-resolution, unmirrored JPEG, stops the camera,
and preserves the previous result until a successful retake. Framing remains one
face, a 4% edge margin, 35–75% face height, and 40–60% box center on both axes.

Reset diagnostics remain in `state.diagnostics`: cumulative reason counts, the
latest 50 events, and aggregate blocked-check counts. The troubleshooting flag
activates after three failed holds or 10 seconds of active guidance. These values
drive the live instructions and troubleshooting. Expand **Capture diagnostics**
to see estimated angles, face height/center, current failed checks, and reset
counts, or download the reset log. Tolerances never relax automatically. Pause/resume, camera
switching, and retry preserve the session log; a new Start after Stop or a retake
begins a new session. Successful metadata includes that session's diagnostics.

Pause, Stop, a hidden page, camera loss/change, or unmount cancels pending work.
Model loading, inference, and encoding cannot publish a late image after cancellation.

Implementation boundaries:

- `src/capture/engine.ts`: UI-independent rules, hold timing, reset diagnostics,
  one-shot commit, and `onState` / `onCaptured` / `onError` callbacks.
- `src/capture/pose.ts` and `gates.ts`: validated column-major matrix conversion
  and face-oval framing. No first-frame neutral calibration or angle smoothing.
- `src/capture/browserSource.ts`: one local VIDEO detector, two-face capacity,
  at most 10 sampled frames/sec, inference at up to 640 px on the longest edge.
  Each full-resolution frame is frozen before deriving its inference canvas.
- `src/hooks/useAutoCapture.ts`: React adapter with state, latest result, and
  start/pause/resume/stop/retake/retry/changeCamera actions. Camera ownership stays
  in `useCamera.ts`; UI components own and revoke download URLs.
- `src/CameraCapture.tsx`: guidance, hold progress, camera controls, diagnostics,
  photo preview and downloads; capture decisions remain in the engine.
- `src/capture/types.ts`: centralized defaults and the versioned
  `FrontCaptureResult` (`image: Blob` plus JSON metadata).

The future Stage 2 adapter can receive `onCaptured(result)` and send image bytes
and metadata through an authenticated backend to the AWS Strands workflow. No
backend, AWS credentials, or Strands execution is included here. A browser Blob URL
is only a local preview reference. See the [active spec](../docs/front-capture-spec.md).

Photos, metadata, and diagnostics stay in page memory. Download any results or
reset logs you need before refreshing or leaving the page.

## Manual photo checkpoint

Open the local Vite URL with `?mode=manual` to use this separate checkpoint.

1. Click **Start camera** and allow camera access.
2. Click **Take photo**. The exact JPEG from `useCamera.capture()` is decoded and
   passed to the browser's local Face Landmarker.
3. Review the face count and the landmark overlay on that snapshot. Use **Show
   original** to compare alignment, or download the original, overlay, or JSON.

The detector loads on the first photo and is reused for later photos. It uses
`IMAGE` mode, up to two faces, transformation matrices enabled, and blendshapes
disabled. One photo is analyzed at a time. This checkpoint uses per-photo analysis independently of the automatic flow.
Both paths currently run inference synchronously on the main browser thread;
worker execution is deferred pending device measurements.

Each JSON report includes a unique `frame_id`, image dimensions, face count,
normalized `x/y/z` landmarks, and transformation matrices. The browser keeps
MediaPipe's web matrix format (`rows`, `columns`, flattened `data`); the separate
Python checkpoint uses nested arrays. A zero-face result is valid and suggests
trying a closer, well-lit photo. Failed analysis preserves the photo and offers
**Retry analysis**.

Camera photos stay in browser memory until deleted or the page is closed. Analysis
does not upload them. Deleting a snapshot releases its download URLs, and leaving
the page releases the camera and detector resources.

The manual integration lives in `src/faceAnalysis.ts` and `src/ManualCapture.tsx`; the existing
`src/hooks/useCamera.ts` still owns the camera stream and capture behavior.

## Browser tests

The app targets current desktop Safari, Firefox, Edge, and Chrome. Photo decoding
uses a DOM image, and MediaPipe receives an explicit DOM canvas so analysis does
not depend on `createImageBitmap` or `OffscreenCanvas`. The camera waits for video
playback before enabling capture; restricted device enumeration does not prevent
use of the active camera.

Install Chrome and Edge normally, then install the two Playwright test engines:

```bash
npx playwright install firefox webkit
npm test                        # all four projects, against the production build
npm test -- --project=firefox    # or chrome, edge, webkit
```

The `edge` project launches Microsoft Edge itself. The `webkit` project uses
Playwright WebKit, Safari's engine, rather than the installed Safari app. This
does not replace physical-device testing in Safari or on iPhone/iPad. The Firefox
project uses Playwright's Firefox build.

Verification after connecting automatic capture to the camera page before UI migration (October 11, 2026):

| Check | Result |
| --- | --- |
| Build and lint | Passed |
| `npm run test:unit` | 21 engine/geometry tests passed |
| `npm run test:auto-ui` | 9 automatic scenarios passed in Chrome |
| `npm run test:auto-real -- --project=chrome` | 3 real local-model scenarios passed |
| `npm test -- --project=chrome` | 7 manual scenarios passed; optional personal-photo test skipped |

Firefox and WebKit passed earlier camera checks; this latest application integration
was verified in Chrome. Edge has not been reverified.

`npm test` covers the separate manual checkpoint. Both automatic browser suites
mount the actual application, at `?mode=front`. The simulated-detector
suite verifies successful capture, frozen pixels, reset logs, retakes,
cancellation, and layout. The real-model suite verifies no-face guidance,
troubleshooting, model-load recovery, and cancellation with local MediaPipe assets.
The mock detector is excluded from normal development and production builds.

```bash
npm run test:unit
npm run test:auto-ui
npm run test:auto-real -- --project=chrome
npm test -- --project=chrome
```

Run the two automatic browser suites sequentially: they share a test output
bundle and port 4174. The production app uses port 4173 during tests.

These tests never turn on the physical webcam. Physical-device trials are still
needed to validate pose direction, tolerance usability, angle jitter, and captured
image quality. The current two-face setting does not use MediaPipe's single-face
smoothing; CPU inference may stall on slower devices. Neither sharpness/exposure
nor liveness/identity checks are part of capture qualification.

Optionally verify a personal JPEG through a simulated camera source:

```bash
CHECKPOINT_PHOTO="/absolute/path/to/your/photo.jpg" npm test -- --grep personal-photo
```

The optional photo and generated screenshots are not committed. The Python
checkpoint continues to run separately from the project root.

For a project-local browser cache, set `PLAYWRIGHT_BROWSERS_PATH` to
`./node_modules/.cache/ms-playwright` for both installation and testing.
`EDGE_EXECUTABLE_PATH` and `FIREFOX_EXECUTABLE_PATH` can select isolated test-browser
executables instead of the default installations.

On macOS 27, Firefox automation may fail before loading the page with
“Could not find profile folder” because its application bookkeeping shares the
protected personal Firefox directory. See the
[upstream Playwright issue](https://github.com/microsoft/playwright/issues/42768).
The local verification used the downloaded Firefox engine with separate
`PlaywrightCameraCheck` application bookkeeping through `FIREFOX_EXECUTABLE_PATH`;
it did not use the personal Firefox profile.

For a manual hardware check, open the app in each installed browser, allow camera
access, use the manual checkpoint to confirm landmark alignment and download results,
switch cameras if available, and stop the camera. Camera permission is separate
for each browser. On the `?mode=front` automatic page, test directions, retakes,
and reset guidance on the actual camera. Test mobile orientation on-device.

## Camera access

Camera logic lives in [`src/hooks/useCamera.ts`](src/hooks/useCamera.ts). It wraps
`navigator.mediaDevices.getUserMedia` and exposes:

| Field | |
| --- | --- |
| `videoRef` | Attach to a `<video autoPlay playsInline muted>` element |
| `status` | `'idle' \| 'starting' \| 'active' \| 'error'` |
| `error` | Human-readable reason the camera couldn't start |
| `devices`, `activeDeviceId` | Available cameras and the one in use |
| `start(deviceId?)`, `stop()` | Turn the camera on/off, or switch cameras |
| `capture(type?, quality?)` | Current frame as a `Blob` (JPEG by default) |

The camera is released automatically when the component using the hook unmounts.

Browsers only allow camera access in a **secure context**: `localhost` works, but to
test from a phone on your LAN the page must be served over HTTPS (e.g. add
[`@vitejs/plugin-basic-ssl`](https://github.com/vitejs/vite-plugin-basic-ssl) and run
`npm run dev -- --host`).
