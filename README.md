# hackMidwest2026
#Frontend

React + TypeScript + Vite app that streams from the device camera, detects face landmarks locally, and captures photos.
```bash
npm ci
npm run dev       # http://localhost:5173
npm run build     # type-check + production build into dist/
npm run lint
npm test          # detector lifecycle tests (Node 24+)
```
The first `npm run dev` or `npm run build` also prepares the local model assets.
It copies the pinned MediaPipe 1.1.0 runtime from `node_modules`, downloads Google's
Face Landmarker model, and verifies its SHA-256. Later runs reuse the checked model.
Internet access is needed for this first setup, but camera frames are processed
on the laptop and are not uploaded. To run setup separately, use `npm run setup:models`.

#Face detection
Choose camera and grant browser camera access. The status beneath the
preview progresses from model loading to Face detected or No face detected.
A green outline traces the detected face, eyes, and mouth. Up to two faces can be
reported; a second face triggers a prompt to keep one person in the frame.

`src/hooks/useFaceLandmarker.ts` connects the existing camera hook to
`src/vision/faceLandmarker.ts`. A dedicated worker in
`public/face-landmarker-worker.js` runs MediaPipe on frames resized to at most
640px wide, at most 10 times per second, with only one frame in flight. The SVG
overlay uses the same aspect-ratio fitting as the video. Stopping the camera,
switching devices, or unmounting the screen disposes the detector and its overlay.

The displayed milliseconds measure one detection call, not total capture or
recognition time. This finds face landmarks; it does not identify a person,
verify identity, or automatically select multi-angle snapshots. Manual photo
capture and the existing download/delete controls remain available.

The repository's Python OpenCV tests validate image conversion/encoding only;
they are not a running face-detection backend and are not needed for this browser
detector. See [Google's MediaPipe Web guide](https://developers.google.com/edge/mediapipe/solutions/vision/face_landmarker/web_js)
for the model API and worker guidance. #GOOGLEROCKS

#Camera access

Camera logic lives in [`src/hooks/useCamera.ts`](src/hooks/useCamera.ts). It wraps
`navigator.mediaDevices.getUserMedia` and exposes:

The camera is released automatically when the component using the hook unmounts.
Browsers only allow camera access in a secure context: `localhost` works, but to
test from a phone on your LAN the page must be served over HTTPS (e.g. add
[`@vitejs/plugin-basic-ssl`](https://github.com/vitejs/vite-plugin-basic-ssl) and run
`npm run dev -- --host`).
