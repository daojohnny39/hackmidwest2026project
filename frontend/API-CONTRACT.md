# CyberPI browser / Member 1 contract — proposal

**Not agreed or connected.** Adeeba confirmed that Member 1’s backend is not ready yet. This is the new Member 5 brief's proposal, prepared October 11, 2026. The older inspected CLI pipeline format in `BACKEND-HANDOFF.md` is different; neither that CLI nor its mock search server establishes these HTTP routes. Member 1 must confirm or adapt the proposal before FE-05 begins.

## Request

```http
POST /api/search
Content-Type: multipart/form-data; boundary=<browser generated>
```

| Part | Type | Frontend draft rule |
| --- | --- | --- |
| `image` | Original JPEG/PNG file | Exactly one selected reference, nonempty, at most 5 MiB; no landmarks/overlay. |
| `firstName` | String | Optional, at most 120 characters; omitted if blank after trimming. |
| `lastName` | String | Optional, at most 120 characters; omitted if blank after trimming. |
| `city` | String | Optional, at most 120 characters; omitted if blank after trimming. |
| `age` | Integer string | Optional, nonnegative whole number; omitted if blank. |
| `authorized` | String `"true"` | Required explicit subject authorization; server must validate independently. |

These field/file limits are frontend defaults for review, not claims about an existing server. Validate MIME **and actual decoded image content** on the server. Camera consent is separate from search authorization and from consent to external processing. Before live upload, provide accurate disclosures about recipient services, retention and deletion, and collect any required additional permission. Do not infer external-processing consent from the existing local-camera checkbox.

`createSearchFormData` builds this payload without sending it. When adding fetch, allow the browser to set the multipart Content-Type boundary. Do not base64-encode images into JSON or put participant fields in query strings.

Suggested asynchronous acknowledgement: HTTP 202.

```json
{ "investigationId": "inv_123", "status": "queued" }
```

## Status and findings

```http
GET /api/search/inv_123
```

```json
{
  "investigationId": "inv_123",
  "status": "completed",
  "candidates": [{
    "id": "cand_001",
    "imageUrl": "https://example.com/photo.jpg",
    "sourceUrl": "https://example.com/profile",
    "pageTitle": "Example public profile",
    "sourceType": "professional_profile",
    "relevance": {
      "classification": "medium",
      "reasons": ["The public name and professional context overlap."],
      "reviewRequired": true
    },
    "threat": { "level": "unknown", "indicators": [] }
  }],
  "warnings": []
}
```

- Status: `queued`, `running`, `completed`, `failed`. Unknown status is an invalid response, never success.
- `completed` must contain an array, including `[]` for a confirmed no-results outcome. Omission or a malformed list is not interpreted as zero findings.
- Candidates may arrive during `running` or remain available after a partial failure. Preserve backend ordering and stable IDs.
- Title, image, source URL, type, relevance and threat may be missing. The local adapter supplies presentation fallbacks; absent/unfamiliar levels remain **unknown**.
- Relevance categories: high, medium, low, unknown. These are contextual evidence labels, not biometric probabilities. Do not translate old confidence numbers into categories without an agreed mapping.
- Threat categories use the same enum but describe the **source**, not the pictured person. Missing threat means **not assessed**; unknown does not mean safe.
- Reasons/indicators must be short public-facing evidence text without private metadata. React renders them as plain text. No source HTML is injected.
- Original source URLs are HTTP(S) only, without embedded credentials. Invalid links are suppressed. New-tab links use `noopener noreferrer` and no-referrer policy.
- Remote thumbnail URLs can make third-party requests. Member 1 should provide appropriately licensed/proxied thumbnail URLs if referrer removal alone is insufficient; frontend URL validation is not a server-side SSRF defense. Current mocks use a bundled illustration only.
- Warnings/errors must not contain secrets or stack traces. The adapter deliberately replaces arbitrary warnings/errors with controlled UI copy. Agree on structured public error codes if the UI needs more specific actions.

Proposed errors: 400 invalid fields, 401/403 authorization, 413 photo limit, 429 rate limit, 5xx service unavailable. HTTP error handling is FE-05; it is not implemented by the payload/parser module.

## Polling proposal

Use polling initially: approximately every 2 seconds after receiving an ID, one request at a time. Stop on `completed`/`failed`, unmount, clear, or a new investigation. Use `AbortController` plus a request-generation guard so late responses cannot repopulate cleared state. Agree on a request timeout and overall deadline with Member 1; a frontend timeout must say the status is unknown and offer a status retry, not report a completed search or silently resubmit a duplicate POST. Honor rate limits/backoff.

Current queued/running/failed states are **static fixtures**, not a running timer or live poller. No network service is called.

## Development configuration to confirm

| Decision | Proposed direction | Status |
| --- | --- | --- |
| Browser route | Relative `/api/search`, `/api/search/:id` | Await Member 1 confirmation. |
| Local backend address | Vite `/api` proxy to backend's local URL | Backend URL unknown; no proxy added. |
| CORS | Same-origin proxy preferred; otherwise allow exact approved frontend origin and only required methods/headers | Not confirmed/configured. |
| Browser authentication | Backend-managed session/short-lived user authorization mechanism | Not confirmed; `authorized=true` is an attestation, not authentication. |
| Provider credentials | Strands/AWS/Jev/search/threat secrets remain on server | No credentials added to UI. Never put them in `VITE_*`. |
| Retention and deletion | Backend must specify actual policy and endpoints | Unknown. Frontend clear does not establish server deletion. |
| Limits, interval, timeout | Validate the above defaults against service behavior | Pending. |

The existing CSP permits only same-origin connections plus local dev WebSockets. Prefer a same-origin proxy; do not relax it to `*` to make integration work. Public configuration is fine in Vite variables; credentials are not.

## Handoff locations

- `src/types/investigation.ts`: frontend interfaces adapted for missing fields.
- `src/services/investigationContract.ts`: validation, multipart payload builder, acknowledgement parser, investigation normalizer and URL validation. No fetch calls.
- `src/mocks/investigationFixtures.ts`: fictional states for independent frontend work.
- `src/components/SubjectForm.tsx`: preview callback currently receives no real request. Replace it with a typed submission callback in FE-03/05 once this contract is confirmed.
- `src/components/ResultsPanel.tsx`: currently selects fixtures. In FE-05/06, lift the actual `InvestigationResult` and request lifecycle into a service/hook, retaining a visibly separate mock path.

Needed from Member 1: confirmed routes/base URL, sanitized successful and failed JSON examples, browser auth method, image limits, polling/timeout policy, and retention/disclosure requirements. No API keys are needed in a frontend handoff.
