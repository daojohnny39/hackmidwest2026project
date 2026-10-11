# CyberPI — frontend preview

Migrated onto `an-frontend-rework` in the original repository.

The Member 5 first assignment: a subject form and source-linked candidate review, built on the team's existing React/TypeScript camera and local MediaPipe UI. The application name remains CyberPI, as confirmed by Adeeba. The green/ash palette, Calibri stack, and three screens are preserved.

**Mock mode only. No search requests or photo uploads.** Member 1’s backend is not ready yet. Camera capture and local landmarks work; candidate findings are fictional and unrelated to the camera photo.

## Run locally

From this checkout's `frontend` folder (Node 26.11.1 verified):

```sh
npm ci
npm run dev -- --host 127.0.0.1 --port 5174 --strictPort
```

Open http://127.0.0.1:5174. Reuse the existing server if that port is occupied. On a fresh checkout, first download `models/face_landmarker.task` using the root README; dev/build copies it and installed WASM files into `public/mediapipe/`. The model is already present in Adeeba's local checkout. No AWS credentials or Python process is needed for this preview.

```sh
npm run build
npm run test:unit
# Reproducible browser suite, with Chrome installed:
npm test -- --project=chrome
```

## Try the first assignment

1. Open camera, accept local capture consent, and allow the browser's camera prompt.
2. Take a reference photo. Keep using the existing original/landmark toggle and delete/capture controls. Up to six photos are allowed; select the desired reference.
3. Enter a name, optionally add a username/public context, and confirm authorization.
4. Choose **Preview sample findings**. The photo is not uploaded. A missing or invalid image, blank name, or absent authorization keeps this action disabled. Failed landmark analysis does not block an otherwise valid photo.
5. Use **Sample scenario** for multiple/one/no findings, partial findings, missing metadata, queued, running, or failed. These are static examples, not a simulated live investigation.
6. **Clear session** discards photos, details and results and stops the camera. Leaving Home also clears the session.

To review candidate cards without a camera, use **View sample**. Source links point to reserved example domains; they are not evidence about real people.

## Implemented now

- Existing camera start/stop, consent, permissions/errors, six-photo capture, device selection and local Face Landmarker integration are reused.
- Subject form with minimal public context, required name and explicit authorization, inline feedback, and a proposed 5 MiB JPEG/PNG limit.
- Candidate thumbnails/fallbacks, page titles, source hostnames and original links, high/medium/low/unknown contextual relevance, explanations and review labels.
- No numeric identity-confidence claims. Relevance does not verify identity. Source risk is shown separately and defaults to not assessed; unknown is not low risk.
- Typed proposed multipart payload builder and defensive response parser. They do not make HTTP requests.
- Fictional fixtures, contract unit tests, and reproducible browser tests for the new UI.

The parser rejects malformed status/completion payloads, preserves incomplete candidates, suppresses unsafe links, and substitutes safe public error copy for arbitrary backend errors. Remote text is rendered as plain React text.

## Preserved team capture work

The latest main branch's automatic capture engine, pose/framing rules, model checksum validation, and camera-disconnection cancellation remain intact. The new CyberPI UI is the default page. Use the **Automatic capture** footer link or `?mode=front` for the separate automatic checkpoint; `?mode=manual` retains the original manual checkpoint. These checkpoints keep their own styles and do not submit photos into the mock investigation form. See [AUTOMATIC-CAPTURE.md](AUTOMATIC-CAPTURE.md).

The new contract tests are in `tests/unit/investigationContract.test.ts`. The original checkpoint tests are preserved as `tests/manual-checkpoint.spec.ts`; `tests/camera.spec.ts` covers the new consent-based UI. The automatic suites now navigate to `?mode=front`.

## Handoff and remaining work

See [FRONTEND-WALKTHROUGH.md](FRONTEND-WALKTHROUGH.md) for the step-by-step explanation, flowcharts and FE-01–FE-12 status.

- [FRONTEND-AUDIT.md](FRONTEND-AUDIT.md): FE-01 architecture, reuse points, image format, browser limitations and current task status.
- [API-CONTRACT.md](API-CONTRACT.md): FE-04 proposal for Member 1. **Not agreed or live.** Includes endpoint shapes, limits, polling, auth/CORS and retention questions.
- [VERIFICATION.md](VERIFICATION.md): checks actually performed and testing limitations.
- [BACKEND-HANDOFF.md](BACKEND-HANDOFF.md): historical inspection of team branches and the older CLI output format, not the newly proposed HTTP API.

Still needed: Member 1's confirmed API/base URL/auth and retention policy; live HTTP client, polling/cancellation, real error handling, optional filters, and an end-to-end backend demo. No backend was modified, no API routes were invented as existing functionality, and no Strands/Jev/threat decision logic was added to the browser.

## Main files

- `src/components/Studio.tsx`: snapshot lifecycle and selected reference.
- `src/hooks/useCamera.ts`, `src/faceAnalysis.ts`, `src/components/PhotoReview.tsx`: existing capture/analysis/review, preserved.
- `src/components/SubjectForm.tsx`: subject details and authorization.
- `src/components/ResultsPanel.tsx`, `CandidateCard.tsx`: mock scenario preview and candidate cards.
- `src/types/investigation.ts`, `src/services/investigationContract.ts`: proposed types, validation, multipart builder and parser.
- `src/mocks/investigationFixtures.ts`: fictional result states.
- `src/index.css`: palette, typography and responsive layout.
- `src/App.tsx`, `src/data/team.ts`: page content and five developer placeholders.

Photos, local analysis and subject fields are kept in temporary tab state, not persistent app storage. The existing CSP blocks external fetch/XHR, including the SDK's background logging. Do not relax it globally. This local-only behavior does not promise deletion or zero retention for future backend services.
