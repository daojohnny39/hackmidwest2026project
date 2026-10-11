# First-assignment verification

> Original UI-development record. The current working copy is now the original repository on `an-frontend-rework`; see [MIGRATION.md](MIGRATION.md) for the migration and current validation.

Date: October 11, 2026. Local checkout: `hackmidwest2026project-ui`, `codex/ui-experience`.

## Completed checks

- `npm run build`: passed (TypeScript and production Vite bundle).
- `npm run test:unit`: **7/7 passed**. Covers required fields/authorization/photo, draft limits, multipart transport, malformed/unknown statuses, incomplete candidates, URL validation, partial findings and safe error handling.
- New test sources type-checked successfully with the installed TypeScript compiler. Chrome Playwright test discovery succeeds. Browser test files were added/updated for reproducibility; **the Playwright runner suite and full browser matrix were not executed in this pass**.
- `git diff --check`: passed.
- Basic credential-pattern scan of frontend TypeScript/TSX and generated JavaScript: no matches for AWS access-key formats, common provider key prefixes, private-key headers or selected secret assignment patterns. This is a targeted check, not a guarantee against every possible secret format. No credentials were added.

## Interactive browser checks

Performed through the Codex in-app browser against the existing local Vite server on `127.0.0.1:5174`:

| Check | Outcome |
| --- | --- |
| Blank name, then blur | Inline name error appears. |
| Name and authorization, no photo | Preview action stays disabled. |
| Clear session | Name, authorization and fixture results reset. |
| Multiple / one / zero findings | Correct counts and distinct empty outcome. |
| Partial findings | Available cards stay visible with a partial-results notice. |
| Missing title, image, source and relevance | Fallback copy; no crash or fabricated score. |
| Queued / running | Explicit sample state; never implies a live search ran. |
| Failed fixture | Separate failed state and sample retry control. |
| Source link | Original example URL and safe new-tab attributes are present. |
| Network-blocked thumbnail | Image fallback appears; source link remains. |
| Consent + synthetic camera | Existing camera UI starts and captures a reference. |
| Network-blocked MediaPipe model | Analysis error preserves the photo; subject form can preview fictional findings. |
| Unblock and retry model | Real local MediaPipe returns zero faces for the synthetic blank/text frame. |
| Delete reference | Form preview becomes disabled. |
| Capture another reference | Capture and local analysis work again. |
| Clear after capture | Photo removed, form reset, synthetic camera track reports `ended`. |
| Request observation during capture/retry/preview/clear | All observed request events used GET; no uploads/POST requests. Event buffer was not truncated. |
| Leave Home and return | Subject fields and sample results reset. |
| Desktop, 390px, 320px | Layout inspected; document width equals viewport width on both narrow checks. |

The synthetic camera override, network blocks and temporary viewport override were removed after testing. No physical camera was activated for these tests. The actual camera hook and MediaPipe analysis implementation were not rewritten.

Preview screenshot saved outside the repository at `../cyberpi-frontend-preview.png` relative to the checkout root (the workspace's `New project` folder).

## Still unverified / future work

- Physical webcam, Safari/WebKit, Firefox and Edge behavior on real devices.
- Browser test suite execution in CI, including the optional approved-photo positive detection checkpoint.
- Live POST/GET endpoints, backend authentication/CORS, real polling, retention/deletion, source discovery, Jev classifications and threat-intelligence output. These are not connected.
- Search quality, biometric identity accuracy, liveness and performance targets. Sample relevance is not a calibrated score or identity finding.

## Reproduce

```sh
npm run build
npm run test:unit
npm test -- --project=chrome
```

For manual UI inspection, start/reuse the dev server, choose **View sample**, and switch **Sample scenario**. Use the camera/form flow only with the subject's authorization. No backend credentials are necessary for this assignment.
