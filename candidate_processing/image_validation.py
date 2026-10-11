"""IMG-02 validation with a bounded subprocess and explicit failure results."""

import asyncio
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Literal

from pydantic import Field, ValidationError, model_validator
import psutil

from .contracts import CandidateImage, Contract, Failure
from .image_worker import INVALID_MESSAGES, MAX_IMAGE_BYTES, MAX_PIXELS, MAX_SIDE
from .storage import AssetIntegrityError, AssetNotFound, LocalAssetStore

MAX_WORKER_OUTPUT = 4096
WORKER_MESSAGES = {
    "image_timeout": "Image decoding exceeded its time limit.",
    "image_memory_limit": "Image decoding exceeded its memory budget.",
    "image_worker_failed": "The image worker exited without a usable result.",
    "invalid_worker_response": "The image worker returned an invalid result.",
    "worker_output_limit": "The image worker exceeded its response limit.",
    "worker_limits_unavailable": "The image worker could not enable resource limits.",
    "worker_monitor_unavailable": "The image worker could not be monitored.",
    "image_decode_failed": "Image decoding could not be completed.",
    "worker_start_failed": "The image worker could not be started.",
}


class ImageValidation(Contract):
    """Decode validation only; no face count, quality score or identity claim."""

    status: Literal["ok", "invalid", "unavailable", "error"]
    valid: bool | None
    mime_type: Literal["image/jpeg", "image/png"] | None = None
    width: int | None = Field(default=None, gt=0, le=MAX_SIDE)
    height: int | None = Field(default=None, gt=0, le=MAX_SIDE)
    byte_length: int | None = Field(default=None, ge=0)
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error: Failure | None = None

    @model_validator(mode="after")
    def coherent_validation(self):
        if self.status == "ok":
            if self.valid is not True or self.error or not self.mime_type or not self.width or not self.height:
                raise ValueError("successful validation requires image measurements and no error")
            if self.byte_length is None or not 1 <= self.byte_length <= MAX_IMAGE_BYTES or not self.content_sha256:
                raise ValueError("successful validation requires bounded byte length and verified digest")
            if self.width * self.height > MAX_PIXELS:
                raise ValueError("decoded image exceeds pixel limit")
        else:
            expected = False if self.status == "invalid" else None
            if self.valid is not expected or self.error is None:
                raise ValueError("unsuccessful validation requires the appropriate validity and a reason")
            if self.mime_type or self.width is not None or self.height is not None:
                raise ValueError("failed validation cannot claim verified image measurements")
        return self


class _WorkerReport(Contract):
    status: Literal["ok", "invalid", "error"]
    code: str | None = None
    mime_type: Literal["image/jpeg", "image/png"] | None = None
    width: int | None = Field(default=None, gt=0, le=MAX_SIDE)
    height: int | None = Field(default=None, gt=0, le=MAX_SIDE)

    @model_validator(mode="after")
    def check_report(self):
        if self.status == "ok":
            if self.code or not self.mime_type or not self.width or not self.height or self.width * self.height > MAX_PIXELS:
                raise ValueError("incomplete worker measurements")
        elif self.mime_type or self.width is not None or self.height is not None:
            raise ValueError("failed worker cannot supply image measurements")
        elif self.code not in (INVALID_MESSAGES if self.status == "invalid" else WORKER_MESSAGES):
            raise ValueError("unknown worker failure")
        return self


class _WorkerFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _worker_command() -> tuple[str, ...]:
    return (sys.executable, "-I", str(Path(__file__).with_name("image_worker.py")))


def _worker_environment() -> dict[str, str]:
    # Do not inherit provider keys, AWS credentials, PYTHONPATH or proxy settings.
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "LANG") if key in os.environ}
    env.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    return env


async def _bounded_output(reader: asyncio.StreamReader, maximum: int) -> bytes:
    chunks, size = [], 0
    while chunk := await reader.read(maximum + 1 - size):
        chunks.append(chunk)
        size += len(chunk)
        if size > maximum:
            raise _WorkerFailure("worker_output_limit")
    return b"".join(chunks)


async def _send_input(writer: asyncio.StreamWriter, data: bytes):
    try:
        writer.write(data)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # The worker may reject input before consuming all of it.
    finally:
        writer.close()


class IsolatedImageValidator:
    def __init__(self, *, timeout_seconds: float = 15, memory_limit_bytes: int = 1024**3):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Worker timeout must be a finite positive number")
        if type(memory_limit_bytes) is not int or memory_limit_bytes <= 0:
            raise ValueError("Worker memory budget must be a positive integer")
        self.timeout_seconds = timeout_seconds
        self.memory_limit_bytes = memory_limit_bytes
        self._lock = asyncio.Lock()

    async def _run_worker(self, data: bytes, *, analyze: bool = False) -> bytes:
        with tempfile.TemporaryDirectory(prefix="facetrace-image-worker-") as directory:
            command = (*_worker_command(), "--analyze") if analyze else _worker_command()
            environment = _worker_environment()
            environment.update(TMPDIR=directory, MPLCONFIGDIR=directory)
            output_limit = 512 * 1024 if analyze else MAX_WORKER_OUTPUT
            try:
                process = await asyncio.create_subprocess_exec(
                    *command, cwd=directory, env=environment,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=MAX_WORKER_OUTPUT,
                )
            except OSError as error:
                raise _WorkerFailure("worker_start_failed") from error
            tasks = [
                asyncio.create_task(_send_input(process.stdin, data)),
                asyncio.create_task(_bounded_output(process.stdout, output_limit)),
                asyncio.create_task(process.wait()),
            ]
            exchange = asyncio.gather(*tasks)
            started = asyncio.get_running_loop().time()
            try:
                try:
                    monitor = psutil.Process(process.pid)
                except psutil.NoSuchProcess:
                    monitor = None
                while not exchange.done():
                    remaining = self.timeout_seconds - (asyncio.get_running_loop().time() - started)
                    if remaining <= 0:
                        raise _WorkerFailure("image_timeout")
                    if monitor is not None:
                        try:
                            if monitor.memory_info().rss > self.memory_limit_bytes:
                                raise _WorkerFailure("image_memory_limit")
                        except psutil.NoSuchProcess:
                            monitor = None
                    await asyncio.wait({exchange}, timeout=min(0.025, remaining))
                if asyncio.get_running_loop().time() - started >= self.timeout_seconds:
                    raise _WorkerFailure("image_timeout")
                _, output, returncode = await exchange
                if returncode != 0:
                    raise _WorkerFailure("image_worker_failed")
                return output
            except psutil.Error as error:
                raise _WorkerFailure("worker_monitor_unavailable") from error
            finally:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                exchange.cancel()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.gather(exchange, return_exceptions=True)
                # Drain the finite buffered output after killing the producer;
                # a paused pipe transport can otherwise prevent wait() ending.
                while await process.stdout.read(MAX_WORKER_OUTPUT):
                    pass
                await process.wait()

    async def _run(self, data: bytes) -> _WorkerReport:
        output = await self._run_worker(data)
        try:
            return _WorkerReport.model_validate_json(output)
        except (ValidationError, ValueError) as error:
            raise _WorkerFailure("invalid_worker_response") from error

    async def validate(self, data: bytes) -> ImageValidation:
        """Decode with limits and return a result; cancellation still propagates."""
        if not isinstance(data, bytes):
            raise TypeError("Image validation requires immutable bytes")
        if not data or len(data) > MAX_IMAGE_BYTES:
            code = "empty_image" if not data else "image_too_large"
            return ImageValidation(status="invalid", valid=False, byteLength=len(data),
                                   error=Failure(stage="validation", code=code, message=INVALID_MESSAGES[code]))
        digest = hashlib.sha256(data).hexdigest()
        async with self._lock:
            try:
                report = await self._run(data)
            except _WorkerFailure as error:
                report = _WorkerReport(status="error", code=error.code)
        if report.status == "ok":
            return ImageValidation(status="ok", valid=True, mimeType=report.mime_type,
                                   width=report.width, height=report.height, byteLength=len(data), contentSha256=digest)
        invalid = report.status == "invalid"
        messages = INVALID_MESSAGES if invalid else WORKER_MESSAGES
        return ImageValidation(
            status=report.status, valid=False if invalid else None, byteLength=len(data), contentSha256=digest,
            error=Failure(stage="validation" if invalid else "image", code=report.code,
                          message=messages[report.code], retryable=report.code in {
                              "image_timeout", "image_worker_failed", "image_decode_failed", "worker_start_failed",
                          }),
        )

    async def validate_candidate(self, image: CandidateImage, store: LocalAssetStore, *, scope: str) -> ImageValidation:
        """Validate retrieved assets or preserve an upstream retrieval failure."""
        loaded = await load_candidate_image(image, store, scope=scope)
        return loaded if isinstance(loaded, ImageValidation) else await self.validate(loaded)


async def load_candidate_image(image: CandidateImage, store: LocalAssetStore, *, scope: str) -> bytes | ImageValidation:
    """Resolve only authorized stored bytes; preserve upstream retrieval errors."""
    if image.status != "available":
        error = image.error or Failure(stage="retrieval", code="image_unavailable", message="No retrieved image is available.")
        return ImageValidation(status="unavailable", valid=None, error=error)
    try:
        _, data = await asyncio.to_thread(store.get, image.asset_id, scope=scope)
    except AssetNotFound:
        error = Failure(stage="storage", code="asset_unavailable", message="The image asset is unavailable in this session.")
    except AssetIntegrityError:
        error = Failure(stage="storage", code="asset_integrity_failed", message="Stored image bytes failed integrity validation.")
    except (OSError, sqlite3.Error):
        error = Failure(stage="storage", code="asset_read_failed", message="The stored image could not be read.", retryable=True)
    else:
        return data
    return ImageValidation(status="unavailable" if error.code == "asset_unavailable" else "error", valid=None, error=error)
