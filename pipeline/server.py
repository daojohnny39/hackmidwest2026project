"""HTTP API for the website (frontend/API-CONTRACT.md): POST /api/search takes the captured photo and
GET /api/search/<id> reports the investigation and its findings.

`python -m pipeline serve --models` runs it on 127.0.0.1:8000, and Vite's dev server proxies /api to
it. Each upload becomes a capture folder, captures/web-<8 hex>/ with manifest.json and the photo,
and a run with stage 0 and stage 1: the per-image Strands agent's search and scrape. findings.py
turns 01_raw_evidence.json into the per-source list with a confidence level. Live search gives no
threat level. Stages 2-4 don't run. In dev mode (`serve --fake-db`) the search runs on the team's fake
image database, and every source's site is benign except one fake fraud site (search/fakedb_backend.py). GET /api/health
names the search backend, so `npm run dev` and `npm run dev:fake` can tell the two servers apart.

The form's `authorized=true` confirms consent, and live search or --models also need
`externalProcessing=true`, the same gate as consent.external_processing in a manifest. The optional
firstName, lastName, city and age become the manifest's subject block (first_name, last_name, city,
age), written in full with "" and null for what was left empty. The per-image agent gets the given
details as context and the findings say where the name and city appear; with none, the search runs
on the photo alone. With --mock the photo isn't saved; the mock internet searches the alex-front
fixture instead.

One search runs at a time; MAX_WAITING more can queue. Investigations live in memory until the
server stops, while captures/ and runs/ keep the photo and evidence like CLI runs (`purge` deletes a
run). There's no authentication, so the server only listens on loopback.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from pipeline import progress
from pipeline.artifacts import create_run, log_event, write_artifact
from pipeline.capture import CaptureError, load_capture
from pipeline.config import Settings
from pipeline.contracts import CaptureArtifact, Subject
from pipeline.findings import Findings, build_findings
from pipeline.hosting import HostingError, image_type
from pipeline.stage1_collect import run_stage1
from pipeline.trace import tracing

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # the frontend's limit
MAX_BODY_BYTES = MAX_IMAGE_BYTES + 64 * 1024
# Form field -> Subject field
SUBJECT_FIELDS = {"firstName": "first_name", "lastName": "last_name", "city": "city"}
MAX_WAITING = 3
MOCK_FIXTURE = "alex-front"
INVESTIGATION_ID = re.compile(r"inv_[0-9a-f]{16}")
NO_STORE = {"Cache-Control": "no-store"}

log = logging.getLogger("pipeline.server")
Search = Callable[[Path], tuple[str, Findings]]  # capture folder -> (run_id, findings)


class ThreatCheck(Protocol):
    """Sets each candidate's `threat`. Dev mode's FakeDbThreats is the only one."""

    def check(self, findings: Findings) -> None: ...


@dataclass
class Investigation:
    investigation_id: str
    status: str = "queued"  # queued, running, completed, failed
    run_id: str | None = None
    findings: Findings | None = None
    stage: progress.SearchStage = "preparing"

    def view(self) -> dict:
        data = {"investigationId": self.investigation_id, "status": self.status}
        if self.status == "running":
            data["stage"] = self.stage
        elif self.status == "completed":
            data["candidates"] = self.findings.candidates
            data["warnings"] = self.findings.warnings
        elif self.status == "failed":
            data["error"] = "search_failed"
        return data


@dataclass
class Investigations:
    items: dict[str, Investigation] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def waiting(self) -> int:
        with self.lock:
            return sum(item.status == "queued" for item in self.items.values())

    def add(self) -> Investigation:
        item = Investigation(f"inv_{secrets.token_hex(8)}")
        with self.lock:
            self.items[item.investigation_id] = item
        return item

    def get(self, investigation_id: str) -> Investigation | None:
        with self.lock:
            return self.items.get(investigation_id)


def run_search(capture_dir: Path, *, settings: Settings, runs_dir: Path,
               threats: ThreatCheck | None = None) -> tuple[str, Findings]:
    """Stage 0 and stage 1 on one capture folder, then the findings, each with a threat level if threats
    is given. Like `run --until 1`."""
    loaded = load_capture(capture_dir, live=settings.live, models=settings.models)
    bundle = loaded.bundle
    run_id, run_dir = create_run(runs_dir, bundle.session_id)
    with tracing(run_dir, 0, name="capture", command="serve", settings=settings.describe(),
                 capture_dir=str(loaded.capture_dir), images=[image.model_dump(mode="json") for image in bundle.images],
                 warnings=loaded.warnings):
        write_artifact(run_dir, 0, CaptureArtifact(
            run_id=run_id, capture=bundle, capture_dir=str(loaded.capture_dir), backend=settings.search_backend,
            scenario=settings.mock_scenario, llm_mode=settings.llm_mode, jev_mode=settings.jev_mode,
            llm_model=settings.llm_model, llm_effort=settings.llm_effort, jev_model=settings.jev_model))
        log_event(run_dir, stage=0, backend=settings.search_backend, status="ok")
    with tracing(run_dir, 1, name="search and scrape", settings=settings.describe(), refresh=False):
        raw = run_stage1(run_dir, settings)
    findings = build_findings(raw, bundle.subject)
    if threats is not None and findings.candidates:
        progress.report("checking")
        with tracing(run_dir, 1, name="threats", sources=len(findings.candidates)):
            threats.check(findings)
    progress.report("finalizing")
    return run_id, findings


def write_capture(captures_dir: Path, data: bytes, subject: Subject, *, external: bool, mock: bool) -> Path:
    """A capture folder for one uploaded photo, in the manifest format capture.py loads."""
    session_id = f"web-{secrets.token_hex(4)}"
    folder = Path(captures_dir) / session_id
    folder.mkdir(parents=True, exist_ok=False)
    image: dict = {"image_id": "front", "angle": "front"}
    if mock:
        image["mock_fixture"] = MOCK_FIXTURE
    else:
        name = "front" + image_type(data)[1]
        (folder / name).write_bytes(data)
        image["file"] = name
    now = datetime.now(timezone.utc).isoformat()
    manifest = {"session_id": session_id, "captured_at": now,
                "consent": {"confirmed": True, "confirmed_at": now, "external_processing": external},
                "subject": subject.model_dump(mode="json"), "images": [image]}
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return folder


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=NO_STORE)


def _consent_problem(form, *, external: bool) -> tuple[int, str] | None:
    if form.get("authorized") != "true":
        return 403, "authorization_required"
    if external and form.get("externalProcessing") != "true":
        return 403, "external_processing_required"
    return None


def _subject(form) -> Subject | None:
    """The optional subject fields as a Subject, or None if one is invalid. Missing or blank is empty."""
    values = {name: form.get(name, "") for name in (*SUBJECT_FIELDS, "age")}
    if not all(isinstance(value, str) for value in values.values()):
        return None
    age = values.pop("age").strip()
    if age and not age.isdigit():
        return None
    try:
        return Subject(**{SUBJECT_FIELDS[name]: value for name, value in values.items()}, age=int(age) if age else None)
    except ValueError:  # pydantic's ValidationError: too long, or an age over 150
        return None


def create_app(settings: Settings, *, runs_dir: Path, captures_dir: Path, search: Search | None = None) -> Starlette:
    threats = None
    if settings.search_backend == "fakedb":
        from pipeline.search.fakedb_backend import FakeDbThreats

        threats = FakeDbThreats()
    search = search or (lambda folder: run_search(folder, settings=settings, runs_dir=runs_dir, threats=threats))
    external = settings.live or settings.models
    mock = settings.search_backend == "mock"
    investigations = Investigations()
    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="search")

    def work(item: Investigation, folder: Path) -> None:
        def update_stage(stage: progress.SearchStage) -> None:
            if item.status == "running":
                item.stage = stage

        item.status = "running"
        try:
            with progress.reporting(update_stage):
                item.run_id, findings = search(folder)
        except Exception:
            log.exception("investigation %s failed", item.investigation_id)
            item.status = "failed"
            return
        item.findings = findings
        item.status = "failed" if findings.failed else "completed"
        log.info("investigation %s (run %s): %s, %d sources", item.investigation_id, item.run_id, item.status,
                 len(findings.candidates))

    async def start(request: Request) -> JSONResponse:
        length = request.headers.get("content-length", "")
        if not length.isdigit():
            return _error(411, "length_required")
        if int(length) > MAX_BODY_BYTES:
            return _error(413, "photo_too_large")
        if investigations.waiting() >= MAX_WAITING:
            return _error(429, "busy")
        async with request.form(max_files=1, max_fields=8) as form:
            problem = _consent_problem(form, external=external)
            if problem:
                return _error(*problem)
            subject = _subject(form)
            if subject is None:
                return _error(400, "invalid_field")
            upload = form.get("image")
            if not isinstance(upload, UploadFile):
                return _error(400, "photo_required")
            data = await upload.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            return _error(413, "photo_too_large")
        try:
            image_type(data)
        except HostingError:
            return _error(400, "unsupported_photo")
        folder = await run_in_threadpool(write_capture, captures_dir, data, subject, external=external, mock=mock)
        try:
            # Decodes the photo fully, so a corrupt file is refused now instead of failing the search.
            await run_in_threadpool(load_capture, folder, live=settings.live, models=settings.models)
        except CaptureError:
            await run_in_threadpool(shutil.rmtree, folder, True)
            return _error(400, "unsupported_photo")
        item = investigations.add()
        accepted = item.view()  # before the worker can move it on
        worker.submit(work, item, folder)
        log.info("investigation %s queued for %s", item.investigation_id, folder.name)
        return JSONResponse(accepted, status_code=202, headers=NO_STORE)

    async def status(request: Request) -> JSONResponse:
        investigation_id = request.path_params["investigation_id"]
        item = investigations.get(investigation_id) if INVESTIGATION_ID.fullmatch(investigation_id) else None
        if item is None:
            return _error(404, "not_found")
        return JSONResponse(item.view(), headers=NO_STORE)

    async def health(_request: Request) -> JSONResponse:
        # Startup probes must not submit a photo or invoke search/model providers.
        return JSONResponse({"service": "cyberpi-search", "status": "ok", "search": settings.search_backend},
                            headers=NO_STORE)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        yield
        worker.shutdown(wait=False, cancel_futures=True)

    return Starlette(routes=[Route("/api/health", health, methods=["GET"]),
                             Route("/api/search", start, methods=["POST"]),
                             Route("/api/search/{investigation_id}", status, methods=["GET"])], lifespan=lifespan)
