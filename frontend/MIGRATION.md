# CyberPI frontend migration

Migration date: October 11, 2026. Branch: `an-frontend-rework`.

## Baseline and scope

- Target: the original `daojohnny39/hackmidwest2026project` repository.
- Final base: latest fetched `main`, `8fb57ffa643b41bb6ead0a86b506e28925699d63`.
- Source: the reviewed `hackmidwest2026project-ui/frontend` working tree, including its uncommitted Member 5 assignment, on top of `f85971e`.
- Three-way migration used the shared frontend baseline `3b672e95a18be32c9cdc10f7caf3355658a9db66`.
- Changes are confined to `frontend/`. Backend code and configuration are unchanged.

The initial migration used main at `565178a`. A final fetch found the team's candidate-image processing merge at `8fb57ff`; the frontend commit rebased onto it without additional conflicts. Its new capture-engine tests are included in the final verification below.

## Conflict resolutions

| File | Resolution |
| --- | --- |
| `README.md` | Describe the new UI, preserve the previous capture guide in `AUTOMATIC-CAPTURE.md`, and document both routes. |
| `package.json` | Keep main's dependency versions and all capture/unit test scripts. Place the added contract tests in main's `tests/unit/` directory. |
| `src/App.tsx` | Use CyberPI as the default UI; retain the previous application as `CaptureCheckpointApp.tsx`. |
| `src/index.css` | Use the CyberPI theme; retain main's original stylesheet as `checkpoint.css`. Load only the selected application's stylesheet. |
| `tests/camera.spec.ts` | Keep the new UI tests here; preserve main's original manual tests as `tests/manual-checkpoint.spec.ts`. |

The shared camera hook merged cleanly. It retains main's camera-disconnection callback and the UI's permission-first device enumeration and session cleanup. The automatic capture engine, pose/framing rules, model-version checks, and asset-preparation script are unchanged from main. Automatic browser tests now explicitly visit `?mode=front`.

## Routes

| URL | Behavior |
| --- | --- |
| `/` or `/#studio` | CyberPI consent, manual capture, subject form, and fictional sample findings. |
| `/#about`, `/#team` | CyberPI information and developer placeholders. |
| `/?mode=front` | Preserved automatic capture checkpoint, also linked in the footer. |
| `/?mode=manual` | Preserved original manual camera checkpoint. |

The capture checkpoints are separate from the new UI's sample investigation form. Search remains mock-only; no live backend connection or photo upload was added.

## Verification performed after migration

- `npm ci` completed using the existing lockfile.
- `npm run build` passed, including TypeScript compilation and local model asset validation.
- `npm run test:unit` passed: 28 tests (21 capture-engine tests from main and seven contract tests).
- Browser test discovery found 23 Chrome tests and nine automatic-capture UI tests. Discovery is not a full runner execution.
- Production preview browser checks used a synthetic, blank camera stream and the real local MediaPipe model. No physical webcam was accessed.
- CyberPI: consent gate, capture, zero-face analysis, subject-form sample preview, and clear-session reset passed; clearing ended the camera track.
- Automatic checkpoint: model loading, no-face guidance, pause, resume, and stop passed; stopping ended the camera track.
- Original manual checkpoint: capture, zero-face analysis, delete, and stop passed.
- Returning from the checkpoints restored the CyberPI Home page and theme. Temporary test camera overrides were removed by navigation.
- Whitespace, unresolved-conflict, and change-scope checks were performed before commit.

Positive-face auto capture and a full browser-suite run were not repeated for this migration. The backend/Python suite was not run because its files were not changed. Earlier verification documents describe their own historical checks; they are not additional migration test results.

## Preserved local work

The source UI checkout was left unchanged. Before updating the original checkout, its older `Adeeba-front-end` work was saved in a local stash named `Before an-frontend-rework migration (original Adeeba-front-end work)`, object `23e600262d6bda2e5846f67311d3a03290cc5269`.

That stash is local recovery data and is not included in the branch. Existing generated caches, model assets and QA artifacts were not committed. The model binary remains ignored; use the root README's setup instructions on a fresh clone.
