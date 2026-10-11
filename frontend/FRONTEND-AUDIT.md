# CyberPI frontend audit — FE-01

> Original UI-development record. The current working copy is now the original repository on `an-frontend-rework`; see [MIGRATION.md](MIGRATION.md) for the migration and current validation.

Inspected October 11, 2026 in `hackmidwest2026project-ui`, branch `codex/ui-experience`, starting from `f85971e` (the UI plus `ba-mediapipe-hookup` merge). No remote branch refresh or backend service validation was performed for this assignment.

## Reuse map

| Concern | Existing implementation | Integration point |
| --- | --- | --- |
| Framework | React 19, TypeScript 6, Vite 8; `src/main.tsx`, `src/App.tsx` | Keep framework and Home/About/Team navigation. |
| Camera lifecycle | `src/hooks/useCamera.ts`: `start`, `stop`, `capture` | Keep permission handling, front camera preference, selected device, playback and track cleanup. |
| Consent | `src/components/ConsentDialog.tsx` | Existing consent covers local camera capture and analysis only. It does not authorize sending an image to external services. |
| Photo ownership | `src/components/Studio.tsx`: `Snapshot`, selected photo, `releaseSnapshot`, `clearSession` | Use the selected snapshot's original `blob` for the proposed single-image request. Never send its landmark overlay instead. |
| Image format | `useCamera.capture()` draws the native video frame to canvas, then `toBlob('image/jpeg', 0.92)` | Stored as a JPEG `Blob` plus a temporary object URL. Preview is mirrored by CSS; captured image is not mirrored. |
| Local MediaPipe | `src/faceAnalysis.ts`: `PhotoAnalyzer`, `PhotoAnalysis` | Reuses the friend's Face Landmarker in IMAGE/CPU mode, up to two faces, with landmarks and transformation matrices. One inference per captured photo. |
| Model assets | `scripts/prepare-vision.mjs`, `public/mediapipe/` | Copies the repository model and installed WASM files locally before dev/build. Keep these paths. |
| Photo review | `src/components/PhotoReview.tsx` | Reuse original/overlay toggle, per-photo selection, face-count guidance, and analysis retry. Analysis failure preserves the original Blob. |
| OpenCV | Root Python tooling (`face_landmarker.py` and Python requirements) | Separate from the browser runtime. No frontend OpenCV hook/import was found; do not duplicate the Python pipeline in React. |
| New subject form | `src/components/SubjectForm.tsx` | Required name, optional username/context, explicit authorization, original-photo validation. Preview only. |
| New results | `src/components/ResultsPanel.tsx`, `CandidateCard.tsx` | Candidate images, source links, categories and uncertainty. Replaces the old illustrative identity-confidence profile. |
| Contract boundary | `src/types/investigation.ts`, `src/services/investigationContract.ts` | Proposed typed payload builder and defensive response normalization, with no network calls. |

## Runtime and privacy behavior

- Captures are manual, at most six per session. Capture/analysis are serialized; the sixth capture stops the camera.
- The subject form uses the selected photo (or the most recent remaining photo). Zero detected faces or a model-load error do not invalidate an otherwise supported photo for a mock preview.
- Photo Blobs, landmark reports, object URLs, and subject fields are held in tab memory. No upload, localStorage, IndexedDB, account, or analytics was added.
- Clear session resets subject details and results, closes the detector, stops video tracks, and revokes image URLs. Leaving Home unmounts this state. `pagehide` also clears it, including the form, for back/forward cache restoration.
- Stopping the camera alone preserves photos. A user can delete a photo, take another, and select the desired reference.
- Local model inference is face/landmark detection, not identity verification. Public-source relevance is a separate concept.
- `index.html` keeps its existing connection CSP. It blocks the MediaPipe SDK's background external logging; a related console denial is expected.

## Local setup

From this checkout's `frontend` folder:

```sh
npm ci
npm run dev -- --host 127.0.0.1 --port 5174 --strictPort
npm run build
npm run test:unit
```

Node 26.11.1 is installed here. The model already exists in this checkout; on a fresh clone, follow the root README to obtain `models/face_landmarker.task`. No Python service or AWS credential is required for the UI. Reuse an existing server on 5174; do not start a second one on the same port.

## Browser and device limitations

- Camera access needs a secure context (localhost or HTTPS), an available device, browser permission, and sometimes macOS camera permission. Another app can hold the device busy.
- Safari/mobile video needs `playsInline` and user interaction; device labels may be unavailable until permission is granted. Enumeration failure is tolerated.
- A still image can be captured only after video data is ready. Native dimensions vary by device; a large image can exceed the draft 5 MiB form limit. There is no automatic resizing yet.
- MediaPipe CPU analysis can briefly block the main thread. This assignment does not change inference or move it to a worker.
- No liveness, automatic head-angle selection, recognition accuracy, or speedup claim is established by this UI.
- Physical camera hardware and the full browser matrix require a separate manual/device pass. Current verification is recorded in `VERIFICATION.md`.

## First-assignment status

- **FE-01:** audit complete.
- **FE-02:** implemented locally, including inline required-field validation and authorization.
- **FE-04:** types, payload builder, and proposal prepared in `API-CONTRACT.md`. Agreement, CORS, base URL, auth, and retention confirmation remain with Member 1.
- **FE-07:** implemented against labeled mock candidates with missing-image/metadata fallbacks.
- FE-03/06/09/10/11 have supporting UI/fixtures; they are not a claim that all acceptance criteria are finished. FE-05, live polling, FE-08 filtering, and FE-12 live end-to-end validation remain future work.
