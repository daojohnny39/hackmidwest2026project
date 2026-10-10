"""Load and validate a capture bundle (spec 0.4): the consent gate for every run.

Consent is checked before any image file is opened. Mock bundles name seed
fixtures and carry no files; live bundles need real JPEG or PNG files inside
the capture folder. This module makes no network calls.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from pydantic import ValidationError

from pipeline.contracts import CaptureArtifact, CaptureBundle, CaptureImage, Consent


class CaptureError(ValueError):
    """The capture folder or manifest can't be used for this run."""


class ConsentError(CaptureError):
    """Consent is missing, not affirmative, or doesn't cover this run's external calls."""


# Image fixture IDs from mock_internet/seed.py; a test keeps the two in sync.
MOCK_FIXTURES: frozenset[str] = frozenset(
    {"alex-front", "alex-left", "alex-right", "sam-front", "casey-front", "unknown"}
)
MAX_IMAGE_BYTES = 10 * 1024 * 1024
RECOMMENDED_SHORT_SIDE = 400


@dataclass(frozen=True)
class LoadedCapture:
    bundle: CaptureBundle
    capture_dir: Path  # resolved
    warnings: list[str]


def load_capture(capture_dir: str | Path, *, live: bool = False, models: bool = False) -> LoadedCapture:
    """Validate a capture folder for this run's mode; raise ConsentError or CaptureError."""
    folder = Path(capture_dir).resolve()
    bundle = _load_bundle(folder, external=live or models)
    warnings: list[str] = []
    for image in bundle.images:
        if live:
            warning = _check_live_image(folder, image)
            if warning:
                warnings.append(warning)
        else:
            _check_mock_image(image)
    return LoadedCapture(bundle=bundle, capture_dir=folder, warnings=warnings)


def recheck_consent(artifact: CaptureArtifact, *, live: bool = False, models: bool = False) -> CaptureBundle:
    """Re-read the source manifest before a stage rerun; refuse if consent no longer holds."""
    folder = Path(artifact.capture_dir)
    try:
        bundle = _load_bundle(folder, external=live or models)
    except ConsentError:
        raise
    except CaptureError as error:
        raise ConsentError(f"can't re-confirm consent: {error}") from error
    saved = artifact.capture
    if bundle.session_id != saved.session_id:
        raise ConsentError(
            f"capture session changed from {saved.session_id!r} to {bundle.session_id!r}; start a new run"
        )
    if bundle.consent.participant != saved.consent.participant:
        raise ConsentError("the manifest's participant changed since this run started; start a new run")
    return bundle


def _load_bundle(folder: Path, *, external: bool) -> CaptureBundle:
    """Read manifest.json, apply the consent gate, then validate the whole bundle."""
    if not folder.is_dir():
        raise CaptureError(f"capture folder not found: {folder}")
    manifest = folder / "manifest.json"
    try:
        info = os.lstat(manifest)
    except FileNotFoundError:
        raise CaptureError(f"no manifest.json in {folder}") from None
    if not stat.S_ISREG(info.st_mode):
        raise CaptureError(f"{manifest} must be a regular file, not a symlink or folder")
    try:
        # utf-8-sig tolerates a BOM from Windows editors.
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CaptureError(f"{manifest} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise CaptureError(f"{manifest} must hold a JSON object")

    _check_consent(data.get("consent"), external=external)
    try:
        return CaptureBundle.model_validate(data)
    except ValidationError as error:
        raise CaptureError(f"invalid manifest: {_describe(error)}") from error


def _check_consent(raw: object, *, external: bool) -> None:
    if not isinstance(raw, dict):
        raise ConsentError("the manifest has no consent block; the participant must agree before anything runs")
    if raw.get("confirmed") is not True:
        raise ConsentError("consent.confirmed is not exactly true; refusing to run")
    if external and raw.get("external_processing") is not True:
        raise ConsentError(
            "--live and --models send data to outside services; consent.external_processing must be exactly true"
        )
    try:
        Consent.model_validate(raw)
    except ValidationError as error:
        raise ConsentError(f"invalid consent block: {_describe(error, prefix='consent')}") from error


def _check_mock_image(image: CaptureImage) -> None:
    label = f"image {image.image_id}"
    if image.mock_fixture is None:
        raise CaptureError(f"{label} has a file but no mock_fixture; real images need --live")
    if image.mock_fixture not in MOCK_FIXTURES:
        known = ", ".join(sorted(MOCK_FIXTURES))
        raise CaptureError(f"{label}: unknown mock_fixture {image.mock_fixture!r} (known: {known})")


def _check_live_image(folder: Path, image: CaptureImage) -> str | None:
    """Validate one real image file; return a warning for small images."""
    label = f"image {image.image_id}"
    if image.mock_fixture is not None:
        raise CaptureError(f"{label}: mock_fixture is for tests only; live mode needs a real image file")
    if not image.file:
        raise CaptureError(f"{label}: live mode needs a file")
    relative = Path(image.file)
    if relative.anchor or not relative.parts or ".." in relative.parts:
        raise CaptureError(f"{label}: file {image.file!r} must be a relative path inside the capture folder")

    # Walk each component with lstat so no symlink can redirect the path, even one
    # pointing back inside the folder.
    path = folder
    for part in relative.parts:
        path = path / part
        try:
            info = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            raise CaptureError(f"{label}: file {image.file!r} not found in {folder}") from None
        if stat.S_ISLNK(info.st_mode):
            raise CaptureError(f"{label}: file {image.file!r} goes through a symlink; copy the image into the folder")
    if not stat.S_ISREG(info.st_mode):
        raise CaptureError(f"{label}: {image.file!r} is not a regular file")
    if info.st_size > MAX_IMAGE_BYTES:
        raise CaptureError(f"{label}: {image.file!r} is {info.st_size} bytes; the limit is {MAX_IMAGE_BYTES}")

    try:
        with Image.open(path, formats=("JPEG", "PNG")) as picture:
            width, height = picture.size
            picture.verify()
        # verify() only reads JPEG headers; a full decode catches truncated files.
        with Image.open(path, formats=("JPEG", "PNG")) as picture:
            picture.load()
    except Exception as error:  # Pillow raises many exception types for corrupt input
        raise CaptureError(f"{label}: {image.file!r} is not a valid JPEG or PNG image ({error})") from error

    short_side = min(width, height)
    if short_side < RECOMMENDED_SHORT_SIDE:
        return f"{label}: short side is {short_side}px; {RECOMMENDED_SHORT_SIDE}px or more is recommended"
    return None


def _describe(error: ValidationError, prefix: str = "") -> str:
    """Turn a pydantic error into one readable line: 'images.1.angle: ...; session_id: ...'."""
    parts = []
    for item in error.errors(include_url=False):
        where = ".".join(str(part) for part in (prefix, *item["loc"]) if part != "") or "manifest"
        parts.append(f"{where}: {item['msg'].removeprefix('Value error, ')}")
    return "; ".join(parts)
