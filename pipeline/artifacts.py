"""Run folders: runs/<run_id>/NN_*.json artifacts, run_log.jsonl, uploads.json, and purge.

trace.py writes the run's debug trace (trace.jsonl) through scrub() below.

Every artifact is validated through contracts.ARTIFACT_ADAPTERS on both write and
read, so a stage never consumes another run's or a malformed output. Writes are
atomic. Logs are sanitized defensively even though callers never pass prompts,
evidence, headers, or signed URLs.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from pipeline.contracts import ARTIFACT_ADAPTERS, STAGE_FILES, UploadRecord, UploadsManifest, safe_id

LOG_FILE = "run_log.jsonl"
UPLOADS_FILE = "uploads.json"
REDACTED = "[REDACTED]"

# Applied in order to every logged string.
_SECRET_PATTERNS = [
    # Credentials in URLs: scheme://user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@"), rf"\1{REDACTED}@"),
    # Authorization header values, including the scheme word
    (re.compile(r"""(?i)\b(authorization["']?\s*[:=]\s*["']?)(?:(?:bearer|basic|token)\s+)?[^\s"',;}]+"""),
     rf"\1{REDACTED}"),
    (re.compile(r"""(?i)\b(bearer\s+)[^\s"',;}]+"""), rf"\1{REDACTED}"),
    # name=value or "name": "value" where the name ends in a secret-like word
    # (api_key, key, access_token, X-Amz-Signature, X-Amz-Credential, X-Amz-Security-Token, Signature, ...).
    # The lookbehind starts names only where a run of name characters starts, which matches the same
    # text but keeps long runs (hex IDs, base64) linear instead of quadratic.
    (re.compile(r"""(?i)(?<![\w-])([\w-]*(?:key|keyid|token|secret|password|passwd|signature|credential|sig)"""
                r"""["']?\s*[=:]\s*["']?)[^\s"'&#,;}]+"""), rf"\1{REDACTED}"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    # Image tunnel hosts and paths (hosting.py): hosted URLs are never logged
    (re.compile(r"(?i)(?<![a-z0-9-])[a-z0-9-]+\.lhr\.life(?:/[^\s\"']*)?"), f"{REDACTED}.lhr.life"),
    # Any remaining URL query string, e.g. the rest of a presigned URL
    (re.compile(r"""(?i)\b([a-z][a-z0-9+.-]*://[^\s?#"']*)\?[^\s#"']*"""), rf"\1?{REDACTED}"),
]
# Dict keys whose scalar values are redacted outright. Matches "api_key" or
# "access_token" but not usage counters such as "input_tokens".
_SENSITIVE_KEY = re.compile(
    r"(?i)(^|[_-])(key|keyid|token|secret|password|passwd|signature|credential|authorization|auth)$"
)

_log_lock = threading.Lock()  # per-image agents may log concurrently


class ArtifactError(ValueError):
    """A run folder or artifact is missing, unsafe, malformed, or belongs to another run."""


def new_run_id(session_id: str) -> str:
    return f"{session_id}-{secrets.token_hex(4)}"


def create_run(runs_dir: Path, session_id: str, *, attempts: int = 5) -> tuple[str, Path]:
    """Create runs/<run_id>/ exclusively, retrying with a fresh ID on collision."""
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(attempts):
        run_id = new_run_id(session_id)
        try:
            safe_id(run_id, "run_id")
        except ValueError as exc:
            raise ArtifactError(str(exc)) from None
        run_dir = runs_dir / run_id
        try:
            run_dir.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return run_id, run_dir
    raise ArtifactError(f"could not create a unique run folder in {runs_dir} after {attempts} attempts")


def _direct_child(root: Path, path: str | Path, what: str) -> Path:
    """Return path resolved, if it is a real (non-symlink) folder directly inside root."""
    root = Path(root).resolve()
    path = Path(path).absolute()
    entry = path.parent.resolve() / path.name
    if entry.is_symlink():
        raise ArtifactError(f"{what} must not be a symlink: {path}")
    entry = entry.resolve()
    if entry.parent != root or not entry.is_dir():
        raise ArtifactError(f"{what} must be a folder directly inside {root}: {path}")
    return entry


def resolve_run_dir(runs_dir: Path, run_path: str | Path) -> Path:
    return _direct_child(runs_dir, run_path, "run folder")


def _adapter(stage: int):
    if stage not in ARTIFACT_ADAPTERS:
        raise ArtifactError(f"unknown stage {stage!r}; known stages: {sorted(ARTIFACT_ADAPTERS)}")
    return ARTIFACT_ADAPTERS[stage]


def _check_run_id(run_dir: Path, run_id: object, what: str) -> None:
    if run_id != Path(run_dir).name:
        raise ArtifactError(f"{what} has run_id {run_id!r} but is in run folder {Path(run_dir).name!r}")


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_artifact(run_dir: Path, stage: int, artifact) -> Path:
    adapter = _adapter(stage)
    path = Path(run_dir) / STAGE_FILES[stage]
    try:
        # Validate, then re-validate the exact bytes that a later read will see.
        payload = adapter.dump_json(adapter.validate_python(artifact), indent=2)
        model = adapter.validate_json(payload)
    except ValidationError as exc:
        raise ArtifactError(f"refusing to write invalid {path.name}: {exc}") from None
    _check_run_id(run_dir, getattr(model, "run_id", None), path.name)
    _write_atomic(path, payload)
    return path


def read_artifact(run_dir: Path, stage: int):
    adapter = _adapter(stage)
    path = Path(run_dir) / STAGE_FILES[stage]
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        raise ArtifactError(f"missing prerequisite {path.name} (stage {stage}) in {run_dir}") from None
    except OSError as exc:
        raise ArtifactError(f"cannot read {path.name}: {exc}") from None
    try:
        model = adapter.validate_json(payload)
    except ValidationError as exc:
        raise ArtifactError(f"{path.name} is malformed or fails its schema: {exc}") from None
    _check_run_id(run_dir, getattr(model, "run_id", None), path.name)
    return model


def invalidate_from(run_dir: Path, stage: int) -> list[Path]:
    """Delete the artifacts of `stage` and every later stage; a stage rerun calls this first."""
    if stage not in STAGE_FILES:
        raise ArtifactError(f"unknown stage {stage!r}")
    deleted = []
    for number, name in sorted(STAGE_FILES.items()):
        if number < stage:
            continue
        path = Path(run_dir) / name
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        deleted.append(path)
    return deleted


def _scrub_text(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def scrub(value, key: str | None = None):
    """Redact secrets in a JSON-like value: secret-named keys, keys and tokens in text, URL query strings."""
    if isinstance(value, dict):
        return {_scrub_text(str(k)): scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(item, key) for item in value]
    if key is not None and _SENSITIVE_KEY.search(key):
        return REDACTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _scrub_text(str(value))


def _endpoint_path(endpoint: str) -> str:
    try:
        return urlsplit(endpoint).path
    except ValueError:
        return "[unparseable endpoint]"


def log_event(run_dir: Path, *, stage: int | str, backend: str | None = None, endpoint: str | None = None,
              status: int | str | None = None, latency_ms: float | None = None, retries: int = 0,
              code: str | None = None, model: str | None = None, usage: dict | None = None) -> None:
    """Append one sanitized JSON line to run_log.jsonl. Fields left as None are omitted."""
    fields = {
        "stage": stage,
        "backend": backend,
        "endpoint": _endpoint_path(endpoint) if endpoint is not None else None,
        "status": status,
        "latency_ms": latency_ms,
        "retries": retries,
        "code": code,
        "model": model,
        "usage": usage,
    }
    event = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
    event.update(scrub({name: value for name, value in fields.items() if value is not None}))
    line = json.dumps(event, ensure_ascii=False) + "\n"
    with _log_lock, open(Path(run_dir) / LOG_FILE, "a", encoding="utf-8") as handle:
        handle.write(line)


def read_uploads(run_dir: Path) -> UploadsManifest:
    """Return the run's cleanup manifest, or an empty one if nothing was uploaded."""
    path = Path(run_dir) / UPLOADS_FILE
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return UploadsManifest(run_id=Path(run_dir).name)
    except OSError as exc:
        raise ArtifactError(f"cannot read {UPLOADS_FILE}: {exc}") from None
    try:
        manifest = UploadsManifest.model_validate_json(payload)
    except ValidationError as exc:
        raise ArtifactError(f"{UPLOADS_FILE} is malformed: {exc}") from None
    _check_run_id(run_dir, manifest.run_id, UPLOADS_FILE)
    return manifest


def write_uploads(run_dir: Path, manifest: UploadsManifest) -> Path:
    _check_run_id(run_dir, manifest.run_id, UPLOADS_FILE)
    for record in manifest.objects:
        values = (record.bucket, record.key, record.version_id or "")
        if any("://" in value or "x-amz-" in value.lower() for value in values):
            raise ArtifactError(f"{UPLOADS_FILE} records bucket and key only, never URLs or signed parameters")
    path = Path(run_dir) / UPLOADS_FILE
    _write_atomic(path, manifest.model_dump_json(indent=2).encode())
    return path


@dataclass
class PurgeReport:
    run_dir: Path
    run_deleted: bool
    remote_failures: list[UploadRecord] = field(default_factory=list)
    capture_dir: Path | None = None  # source capture folder recorded in 00_capture.json, if readable
    capture_deleted: bool = False
    messages: list[str] = field(default_factory=list)


def _recorded_capture_dir(run_dir: Path) -> Path | None:
    if (run_dir / STAGE_FILES[0]).is_symlink():
        return None
    try:
        return Path(read_artifact(run_dir, 0).capture_dir)
    except ArtifactError:
        return None


def _delete_capture(capture_dir: Path | None, captures_dir: Path | None) -> tuple[bool, str]:
    """Delete the recorded capture folder only if it sits directly inside captures_dir."""
    if capture_dir is None:
        return False, "source capture retained: 00_capture.json is missing or unreadable, so its folder is unknown"
    if captures_dir is None:
        return False, f"source capture retained: {capture_dir} (no captures folder given)"
    try:
        folder = _direct_child(captures_dir, capture_dir, "capture folder")
    except ArtifactError as exc:
        return False, f"source capture retained: {exc}"
    parts = folder.parts
    if any(parts[i:i + 2] == ("tests", "fixtures") for i in range(len(parts) - 1)):
        return False, f"source capture retained: {folder} is a test fixture"
    shutil.rmtree(folder)
    return True, f"deleted source capture {folder}"


def purge_run(runs_dir: Path, run_path: str | Path, *, delete_remote: Callable[[UploadRecord], None] | None = None,
              include_capture: bool = False, captures_dir: Path | None = None) -> PurgeReport:
    """Delete uploaded objects, then the run folder. Needs no consent and no API keys.

    Local data is deleted even if remote cleanup fails; only a minimal uploads.json
    listing the undeleted objects is kept so `purge` can be retried.
    """
    run_dir = resolve_run_dir(runs_dir, run_path)
    report = PurgeReport(run_dir=run_dir, run_deleted=False, capture_dir=_recorded_capture_dir(run_dir))
    keep: set[str] = set()

    try:
        manifest = read_uploads(run_dir)
    except ArtifactError as exc:
        manifest = None
        keep.add(UPLOADS_FILE)
        report.messages.append(f"kept {UPLOADS_FILE} for manual cleanup; uploaded images may remain: {exc}")

    if manifest is not None:
        for record in manifest.objects:
            if record.deleted:
                continue
            if delete_remote is None:
                report.remote_failures.append(record)
                continue
            try:
                delete_remote(record)
            except Exception as exc:
                report.remote_failures.append(record)
                report.messages.append(f"could not delete s3://{record.bucket}/{record.key}: "
                                       f"{type(exc).__name__}: {_scrub_text(str(exc))}")
            else:
                record.deleted = True
        if manifest.objects:
            # Persist the outcome before touching local files, so a crash below leaves an accurate record.
            write_uploads(run_dir, UploadsManifest(run_id=run_dir.name, objects=report.remote_failures))
        if report.remote_failures and delete_remote is None:
            report.messages.append("no remote deletion client was given for uploaded images")
        if report.remote_failures:
            keep.add(UPLOADS_FILE)
            report.messages.append(f"{len(report.remote_failures)} uploaded image(s) not deleted; "
                                   f"kept {run_dir / UPLOADS_FILE}. Retry `purge` to delete them.")

    for entry in run_dir.iterdir():
        if entry.name in keep:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)  # does not follow symlinks inside the tree
        else:
            entry.unlink()
    if not keep:
        run_dir.rmdir()
        report.run_deleted = True

    if include_capture:
        report.capture_deleted, message = _delete_capture(report.capture_dir, captures_dir)
        report.messages.append(message)
    else:
        report.messages.append(f"source capture retained: {report.capture_dir or 'unknown'}")
    return report
