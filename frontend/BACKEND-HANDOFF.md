# CyberPI frontend / backend handoff

> Historical branch inspection. The October 11 CyberPI frontend assignment uses the new **proposed** candidate contract in [API-CONTRACT.md](API-CONTRACT.md). That proposal is not yet agreed or connected; the older CLI confidence/profile shape below does not map automatically to contextual relevance.

Repository inspection: October 10, 2026. This records the pushed branches inspected; it does not validate or run their live services.

## Existing team work

| Branch | Inspected commit | Integration status |
| --- | --- | --- |
| main | 86a787bc33457901fa3c443515c892e9e4543eaf | Camera UI and OpenCV setup; no application HTTP API found. |
| Johnny | 677e43ffb7b9f977f62be7b2dd59755a49139cfe | Strands/search pipeline and result contracts. CLI consumes a local capture folder and writes run artifacts. |
| ba-mediapipe-hookup | 3b672e95a18be32c9cdc10f7caf3355658a9db66 | Integrated into the local CyberPI UI: per-photo face counts, landmark overlays, reports, and retry/cleanup controls. |
| jun-search | 43d461250e720e77c8898fe3e71505d3553b7213 | Search work present; the agent and fake-server entrypoint files are empty. |

The `Johnny` mock internet server is a fictional search fixture service on port 8765. Its GET routes are not an endpoint for submitting participant camera photos to the complete pipeline.

## Contract already available

On `Johnny`, see `pipeline/contracts.py` and `pipeline/__main__.py`.

Capture metadata contains:
- `session_id`, `captured_at`
- `consent.confirmed`, `consent.participant`, `consent.confirmed_at`, `consent.external_processing`
- `images` containing `image_id`, `file`, `angle`, optional yaw/pitch/roll

External-processing consent must not be inferred from this UI's existing local-camera permission. The UI's capture prompts are manual guidance; they do not establish measured angles.

Local `Snapshot.report` now contains the friend's `PhotoAnalysis` format: frame ID, dimensions, face count, landmarks, and transformation matrices. It remains browser-local. `index.html` blocks external fetch/XHR connections, including the SDK's default logging; an approved remote API will need its exact origin added to `connect-src` along with the consent/retention work below.

The final output is one of:
- `status: "match"`: `run_id`, sourced `name`, optional sourced `role`, `workplace`, `location`, `bio`, `accounts`, `confidence`, `method`, `flags`, `generated_at`.
- `status: "no_match"`: `run_id`, `reason`, `top_candidates`, `generated_at`.

A sourced field is `{ "value": "...", "sources": ["https://..."] }`.
Backend confidence is **0–1**; UI percentages require multiplying by 100. Preserve `method` (`jev` or `fallback`), warning flags, and the no-match/review outcome. These are uncalibrated scores, not verified identity accuracy. Do not convert a top candidate from a no-match response into a confirmed result.

## Missing connection

No HTTP route for uploading a capture bundle and returning the pipeline result was found in these branches. The backend owner needs to supply:
1. The upload URL and method, image/metadata encoding, file limits, and authentication requirements.
2. Whether processing returns immediately or exposes a job ID with a status endpoint.
3. Error and cancellation behavior, and a session-clear endpoint if data is retained server-side.
4. Approved disclosure of external processing and actual retention/deletion behavior.

The browser should call that application endpoint. Bedrock, SerpApi, or other provider credentials must stay on the server; they are not browser API addresses or frontend configuration.

## Retention behavior to resolve

The current `Johnny` implementation writes capture/run records and evidence under `runs/`. Source images remain in their capture directory. Live image search uses temporary S3 uploads with cleanup, and the CLI provides a `purge` command; ordinary purge can retain the original capture folder unless explicitly included. This is not an automatic zero-retention system.

CyberPI's current consent wording describes its local-only preview. Before enabling uploads, the team must implement its intended retention policy and update consent to match. Do not promise deletion merely because a frontend state array was cleared.

The UI remains local-only with labeled fictional results until this connection is provided. No exposed chat credential was used or added to the project.
