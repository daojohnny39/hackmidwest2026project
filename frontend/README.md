# Frontend

React + TypeScript + Vite app that streams from the device camera and captures photos.
Each captured photo is analyzed locally with MediaPipe Face Landmarker in the browser.

First download `models/face_landmarker.task` in the project root using Step 3 of
the [project README](../README.md). Then run these commands from `frontend/`:

```bash
npm install
npm run dev       # http://localhost:5173
npm run build     # type-check + production build into dist/
npm run lint
```

`npm run dev` and `npm run build` automatically copy the model and the installed
MediaPipe WebAssembly files to `public/mediapipe/`. That generated directory is
ignored by Git. Production builds include these assets in `dist/`; no CDN or
Python analysis service is needed at runtime.

## Photo analysis

1. Click **Start camera** and allow camera access.
2. Click **Take photo**. The exact JPEG from `useCamera.capture()` is decoded and
   passed to the browser's local Face Landmarker.
3. Review the face count and the landmark overlay on that snapshot. Use **Show
   original** to compare alignment, or download the original, overlay, or JSON.

The detector loads on the first photo and is reused for later photos. It uses
`IMAGE` mode, up to two faces, transformation matrices enabled, and blendshapes
disabled. One photo is analyzed at a time. This is a per-photo checkpoint, not
continuous video analysis. Inference runs synchronously in the browser for each
photo; continuous processing would need a separate scheduling/worker design.

Each JSON report includes a unique `frame_id`, image dimensions, face count,
normalized `x/y/z` landmarks, and transformation matrices. The browser keeps
MediaPipe's web matrix format (`rows`, `columns`, flattened `data`); the separate
Python checkpoint uses nested arrays. A zero-face result is valid and suggests
trying a closer, well-lit photo. Failed analysis preserves the photo and offers
**Retry analysis**.

Camera photos stay in browser memory until deleted or the page is closed. Analysis
does not upload them. Deleting a snapshot releases its download URLs, and leaving
the page releases the camera and detector resources.

The integration lives in `src/faceAnalysis.ts` and `src/App.tsx`; the existing
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

Production-build verification on October 10, 2026 (macOS 27.0.1), including the
optional personal-photo checkpoint:

| Test browser | Result |
| --- | --- |
| Google Chrome 154.0.8037.98 | 8 scenarios passed |
| Playwright Firefox 157.0 | 8 scenarios passed |
| Playwright WebKit 27.2 | 8 scenarios passed |
| Microsoft Edge 155.0.4283.45 | Unverified: browser exits before loading the app |

The temporary Edge copy reported `Failed to get the path for 1001` at startup;
macOS also denied creation of its application-data directory. This is a test
environment limitation, not a passing Edge result. Run the `edge` project on a
machine where Microsoft Edge can launch. Installed Safari and physical mobile
devices have not yet been manually verified.

Eight scenarios per browser cover capture, result association, downloads,
detector reuse, deletion during loading, model-load recovery, operation without
ImageBitmap/OffscreenCanvas, restricted device enumeration, permission denial,
playback-failure cleanup, and the optional positive photo checkpoint. The capture
flow also checks for uploads and external requests. The tests use simulated camera
inputs and the actual local model; they do not turn on the physical webcam.

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
access, capture a photo, confirm the overlay alignment, download the results,
switch cameras if available, and stop the camera. Camera permission is separate
for each browser. Test iPhone/iPad camera switching and orientation on the device.

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
