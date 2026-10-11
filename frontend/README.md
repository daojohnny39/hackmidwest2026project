# CyberPI — website

Migrated onto `an-frontend-rework` in the original repository.

The Member 5 first assignment: a subject form and source-linked candidate review, built on the team's existing React/TypeScript camera and local MediaPipe UI. The application name remains CyberPI, as confirmed by Adeeba. The green/ash palette, Calibri stack, and three screens are preserved.

The website captures a reference photo locally, then sends it to the Python search API when you choose **Search for my photo** and confirm both consent checkboxes. **View sample** remains a separate preview with fictional findings.

## Run locally

From this checkout's `frontend` folder (Node 26.11.1 verified):

```sh
npm ci
npm run dev -- --host 127.0.0.1 --port 5174 --strictPort
```

Open http://127.0.0.1:5174. `npm run dev` starts the Python search API on port 8000, waits for `/api/health`, then starts Vite. It reuses an already-running CyberPI API. Ctrl-C stops processes started by this command; an existing API remains running.

On a fresh checkout, install `requirements.txt` into the repository `.venv`, configure the search credentials in `.env` (see [agent pipeline setup](../docs/agent-pipeline.md)), and download `models/face_landmarker.task` using the root README. The launcher uses `.venv/bin/python` (`.venv/Scripts/python.exe` on Windows), falling back to `python3`; `CYBERPI_PYTHON` selects another interpreter. It starts `pipeline serve --models` with the repository configuration. Starting the servers and checking readiness do not submit a photo or run a search.

`npm run dev:fake` is dev mode: the same website searching the team's fake image database instead of the internet. It starts `pipeline serve --models --fake-db` on port 8001 and Vite on port 5180 (unless you pass `--port`), so it can run next to `npm run dev`. See [dev mode](../docs/agent-pipeline.md#dev-mode-the-fake-image-database).

To use a separately managed API, set `CYBERPI_API_URL=http://127.0.0.1:8000` before `npm run dev`. A custom API must already be running. For camera/UI-only work, use `npm run dev:frontend`; live searches still require a separate API. `npm run preview` also requires that API. Both Vite modes proxy `/api` to it.

```sh
npm run build
npm run test:unit
# Reproducible browser suite, with Chrome installed:
npm test -- --project=chrome
```

## Try the first assignment

1. Automatic capture is selected by default. Choose **Start camera**, accept local capture consent, and allow the browser's camera prompt.
2. Follow the face-position and angle guidance; a qualifying 400 ms hold automatically captures one reference and stops the camera. Alternatively, uncheck **Automatic capture** beside **Mirror preview**, open the camera, and choose **Take photo** once. Retaking replaces the single reference. Manual mode includes the original/landmark toggle.
3. Optionally provide First Name, Last Name, City, and Age, and confirm authorization. The information icon beside **Subject details** explains how these fields help narrow results.
4. Confirm external processing consent and choose **Search for my photo**. The photo is uploaded to the API, and the website polls for findings. A missing or invalid image or absent consent keeps this action disabled. Failed manual landmark analysis does not block an otherwise valid photo.
5. Alternatively, choose **View sample** and use **Sample scenario** for fictional result states. These are static examples, not a live investigation.
6. **Clear session** discards photos, details and results and stops the camera. Leaving Home also clears the session.

To review candidate cards without a camera, use **View sample**. Source links point to reserved example domains; they are not evidence about real people.

## Implemented now

- Automatic capture by default, with a manual option requiring only one photo; both supply the reference to the subject form. Consent, permissions/errors and local Face Landmarker integration are reused.
- Subject form with optional first name, last name, city and age, an explanation icon and explicit authorization, inline feedback, and a proposed 5 MiB JPEG/PNG limit.
- Candidate thumbnails/fallbacks, page titles, source hostnames and original links, high/medium/low/unknown contextual relevance, explanations and review labels.
- No numeric identity-confidence claims. Relevance does not verify identity. Source risk is shown separately and defaults to not assessed; unknown is not low risk.
- Typed multipart payload builder, HTTP search client, polling/cancellation, and defensive response parser.
- Fictional fixtures, contract unit tests, and reproducible browser tests for the new UI.

The parser rejects malformed status/completion payloads, preserves incomplete candidates, suppresses unsafe links, and substitutes safe public error copy for arbitrary backend errors. Remote text is rendered as plain React text.

## Preserved team capture work

The latest main branch's automatic capture engine, pose/framing rules, model checksum validation, and camera-disconnection cancellation remain intact. Automatic capture is integrated into the default CyberPI page and subject form. Uncheck **Automatic capture** beside **Mirror preview** or use `?capture=manual` for the one-photo manual flow. Switching capture modes clears the session and preserves the mirror setting. The **Automatic capture** footer link opens the current app with automatic capture selected. Use `?mode=front` for the separate automatic checkpoint; `?mode=manual` retains the original manual checkpoint. These checkpoints keep their own styles and do not submit photos into the mock investigation form. See [AUTOMATIC-CAPTURE.md](AUTOMATIC-CAPTURE.md).

The new contract tests are in `tests/unit/investigationContract.test.ts`. The original checkpoint tests are preserved as `tests/manual-checkpoint.spec.ts`; `tests/camera.spec.ts` covers the new consent-based UI. The automatic suites now navigate to `?mode=front`.

## Handoff and remaining work

See [FRONTEND-WALKTHROUGH.md](FRONTEND-WALKTHROUGH.md) for the step-by-step explanation, flowcharts and FE-01–FE-12 status.

- [FRONTEND-AUDIT.md](FRONTEND-AUDIT.md): FE-01 architecture, reuse points, image format, browser limitations and current task status.
- [API-CONTRACT.md](API-CONTRACT.md): connected API routes, limits, polling, and remaining deployment questions.
- [VERIFICATION.md](VERIFICATION.md): checks actually performed and testing limitations.
- [BACKEND-HANDOFF.md](BACKEND-HANDOFF.md): historical inspection of team branches and the older CLI output format, not the newly proposed HTTP API.

The API currently listens on loopback for local development. Authentication and retention policy for a hosted deployment remain future work. Search and model processing run on the Python server.

## Main files

- `src/components/Studio.tsx`: default automatic capture, capture mode selection and reference lifecycle.
- `src/components/ManualStudio.tsx`: one-photo manual capture and local landmark review.
- `src/hooks/useCamera.ts`, `src/faceAnalysis.ts`, `src/components/PhotoReview.tsx`: existing capture/analysis/review, preserved.
- `src/components/SubjectForm.tsx`: subject details and authorization.
- `src/components/ResultsPanel.tsx`, `CandidateCard.tsx`: mock scenario preview and candidate cards.
- `src/types/investigation.ts`, `src/services/investigationContract.ts`: proposed types, validation, multipart builder and parser.
- `src/mocks/investigationFixtures.ts`: fictional result states.
- `src/index.css`: palette, typography and responsive layout.
- `src/App.tsx`, `src/data/team.ts`: page content and team names/photos in Developer 01–05 order.

Photos, local analysis and subject fields are kept in temporary tab state until submitted. After a live search, the Python server retains captures and run artifacts as described in the pipeline documentation; **Clear session** only clears browser state. The existing CSP allows the same-origin `/api` proxy and blocks external fetch/XHR, including the SDK's background logging.
