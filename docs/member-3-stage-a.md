# Your Stage A: image processing

Stage A (IMG-01 through IMG-04) is one review/commit checkpoint, as requested.
The implementation and automated checks are complete. Real-photo acceptance
and quality-threshold calibration remain explicit handoff checks.

## Task coverage

| Your MD task | Implementation |
| --- | --- |
| IMG-01: inspect existing code | Audit in the implementation plan; reuse `create_face_landmarker()` and the checkpoint's shared BGR-to-RGB inference helper. Browser capture still exports the existing JPEG/metadata. |
| IMG-02: validate candidates | JPEG/PNG content checks, dimensions, byte/pixel limits, corruption and animation rejection, isolated decoding and structured failure reasons. Retrieval failures remain separate from invalid images. |
| IMG-03: detect and preprocess | Zero/one/two face outcomes, detector-capacity flag, padded normalized crop assets, unchanged originals, preserved candidate/source IDs and URLs, per-candidate failures. |
| IMG-04: assess quality | Dimensions, measured blur/exposure, possible edge clipping, and review flags. Poor or uncertain quality never deletes the candidate. |

The contract and private asset store are included as prerequisites. Jev,
prioritization, web discovery, reputation checks and HTTP/Strands adapters remain
outside this commit. The returned classification is explicitly skipped/unknown.

## Callable interface

```python
from candidate_processing.contracts import CandidateInput
from candidate_processing.image_analysis import IsolatedImageAnalyzer
from candidate_processing.storage import LocalAssetStore

store = LocalAssetStore()  # FACETRACE_DATA_DIR or .facetrace-data/
analyzer = IsolatedImageAnalyzer()

async def process_candidate(candidate_json, trusted_scope):
    candidate = CandidateInput.model_validate(candidate_json)
    result = await analyzer.analyze_candidate(candidate, store, scope=trusted_scope)
    return result.model_dump(mode="json", by_alias=True)
```

Member 2 retrieves image bytes. The backend stores them with `store.put()` and
places the returned ID in `image.assetId`. The backend supplies the trusted
caller/session scope; a subject name is not an authorization scope. Candidate
URLs are provenance and are never fetched by this module. See the
[contract guide](../contracts/README.md) and [storage guide](member-3-storage.md).

`IsolatedImageValidator.validate_candidate()` is also available when only decode
validation is needed. The analyzer validates and detects within the same worker,
so callers do not need to run validation separately first.

## Validation and resource limits

| Boundary | Implemented limit |
| --- | --- |
| Compressed bytes | 1 byte through 10 MiB; reject oversized input before starting a worker |
| Decoded image | Still JPEG/PNG only; up to 20 million pixels and 10,000 px on either side |
| Corruption | Check format signature, complete end markers, file verification and full pixel decoding; these are image checks, not an arbitrary appended-content scanner |
| Normalization | Apply EXIF orientation, convert grayscale/palette/CMYK to RGB, flatten transparency over white; original bytes remain immutable |
| Worker | Disposable Python process, one at a time per analyzer; 15-second wall deadline after launch, 10-second CPU limit, core dumps disabled |
| Memory | Parent samples worker RSS every 25 ms and terminates above 1 GiB; Linux additionally caps address space at 2 GiB |
| Response | At most 4 KiB for validation or 512 KiB for analysis, with validated metadata and at most two encoded crops |

The worker receives no provider/AWS credentials from the parent environment and
runs in Python isolated mode with a temporary working directory. Timeouts,
crashes, excess output, monitor failures and cancellation terminate/reap it; a
later candidate gets a fresh worker. No model instance is reused across worker
processes, so model startup contributes to per-image latency.

This is a process/resource boundary, not an OS network/filesystem sandbox. It
does not intentionally access the network. macOS RSS polling can temporarily
overshoot the threshold; a production host must supply hard memory and OS access
restrictions before exposing arbitrary uploads. Linux limits are implemented but
were not exercised on this macOS development host. Unsupported platforms or
failed limit setup return a failure rather than silently running without limits.

Pillow's [image API](https://pillow.readthedocs.io/en/stable/reference/Image.html)
documents lazy opening, full loading and decompression-bomb checks. Python's
[resource API](https://docs.python.org/3.13/library/resource.html) documents the
platform-dependent limits used here. Installed versions and local behavior were
checked on October 11, 2026.

## Faces, crops and quality

Inference uses the existing CPU MediaPipe Face Landmarker, with an image scaled
to at most 640 px on its longest side. Coordinates refer to the full-size,
EXIF-oriented image. Each landmark bounding box gets 15% padding and is clipped
to image bounds. Its crop is aspect-preserved and padded to 256 × 256, encoded
as JPEG, and saved as a separate asset in the original caller's scope.

`faceCount: 0` means detection succeeded without finding a face; failed detection
uses `null`. Two detected faces set `atDetectorLimit: true`; the true total could
be larger. Crops are unassigned faces, not biometric matches to the subject.
If cropping/storage fails, the original and detected-face count survive with a
failure reason. Cancelling a crop write cleans up newly saved crop assets.

Quality measurements use the grayscale inference image:

| Heuristic | Initial review threshold |
| --- | --- |
| Possible blur | Laplacian variance below 50 |
| Underexposure | Mean luminance below 40, or over 60% of pixels below intensity 30 |
| Possible partial face | A detected landmark lies within 1% of an image edge or beyond it |

These thresholds are provisional, exercised with synthetic extreme cases; they
are not calibrated accuracy guarantees. Full-frame blur can be affected by a
plain background, and edge landmarks do not prove occlusion. Concerns yield
`quality: poor`; otherwise quality stays `unknown`, with
`quality_thresholds_uncalibrated`. No image is labeled `good` before calibration.
Every Stage A result remains reviewable, including for pending Stage B
classification. Existing camera capture tolerances and timing are unchanged.

## Verification and remaining acceptance

Automated checks cover 20 contract cases, 21 storage cases, 22 validation cases
and 17 face/crop/quality cases. The existing photo-checkpoint regression suite
checks the shared-helper refactor separately.

- Valid JPEG/PNG, EXIF, grayscale, alpha, palette and CMYK; malformed, oversized,
  animated and truncated files; byte/pixel boundary values.
- Worker timeout, memory budget, excessive/malformed output, crash, cancellation,
  secret-environment filtering, and recovery on the next image.
- Controlled detector outputs for zero/one/two faces, crop coordinates and size,
  partial/invalid landmarks, detection and crop-storage failures, source
  preservation, review reasons and synthetic quality measurements.
- A real local MediaPipe run on a blank image confirms zero faces and recovery
  after an invalid image. One/two-face behavior uses controlled detectors in
  automated tests; real-photo acceptance with authorized examples remains.

Before claiming quality accuracy, check consenting/authorized one-face,
multi-face, blurred, dark and clipped-face examples on the actual runtime and
record threshold decisions. The existing physical-camera acceptance trial is
also still pending. No Jev call or identity matching is performed by these tests.

Run the checks and use the combined staging list in the
[Stage A commit guide](member-3-commit-sections.md). Pause after this commit
checkpoint; Stage B is the next implementation work.
