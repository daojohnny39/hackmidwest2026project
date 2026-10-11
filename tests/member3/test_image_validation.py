"""IMG-02 acceptance with synthetic codecs and disposable failure workers."""

import asyncio
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import zlib

from PIL import Image
import psutil

from candidate_processing.contracts import CandidateImage, Failure
from candidate_processing.image_validation import IsolatedImageValidator
from candidate_processing.image_worker import MAX_IMAGE_BYTES, decode_rgb
from candidate_processing.storage import LocalAssetStore


def encoded_image(format="PNG", mode="RGB", size=(32, 24), **options):
    with Image.new(mode, size) as image:
        output = BytesIO()
        image.save(output, format=format, **options)
        return output.getvalue()


def oversized_header(width, height):
    data = encoded_image()
    header = struct.pack(">II", width, height) + data[24:29]
    checksum = struct.pack(">I", zlib.crc32(b"IHDR" + header))
    return data[:16] + header + checksum + data[33:]


def test_command(script):
    return (sys.executable, "-I", "-c", script)


class ImageValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.validator = IsolatedImageValidator()

    async def test_jpeg_and_png_measurements_and_digest(self):
        for format, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg")):
            with self.subTest(format=format):
                data = encoded_image(format)
                result = await self.validator.validate(data)
                self.assertEqual(result.status, "ok")
                self.assertTrue(result.valid)
                self.assertEqual((result.width, result.height), (32, 24))
                self.assertEqual(result.mime_type, mime)
                self.assertEqual(result.byte_length, len(data))
                self.assertEqual(result.content_sha256, hashlib.sha256(data).hexdigest())
                self.assertIsNone(result.error)
                self.assertNotIn("faceCount", result.model_dump(by_alias=True))

    async def test_unknown_formats_and_nonimage_bytes_are_rejected(self):
        for data in (encoded_image("GIF"), encoded_image("BMP"), b"not an image"):
            with self.subTest(prefix=data[:4]):
                result = await self.validator.validate(data)
                self.assertEqual(result.status, "invalid")
                self.assertFalse(result.valid)
                self.assertEqual(result.error.code, "unsupported_image_format")
                self.assertFalse(result.error.retryable)

    async def test_corruption_and_truncation_fail_full_validation(self):
        png, jpeg = encoded_image(), encoded_image("JPEG")
        damaged = bytearray(png)
        damaged[-20] ^= 1  # Corrupt image data while preserving its end marker.
        for data in (png[:-12], jpeg[:-2], bytes(damaged), jpeg[:40] + jpeg[-2:]):
            with self.subTest(length=len(data)):
                result = await self.validator.validate(data)
                self.assertEqual(result.status, "invalid")
                self.assertEqual(result.error.code, "corrupt_image")
                self.assertIsNone(result.width)

    async def test_empty_and_oversized_bytes_do_not_start_a_worker(self):
        with patch("candidate_processing.image_validation.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
            for data, code in ((b"", "empty_image"), (b"x" * (MAX_IMAGE_BYTES + 1), "image_too_large")):
                result = await self.validator.validate(data)
                self.assertEqual(result.error.code, code)
            spawn.assert_not_awaited()

    async def test_dimensions_and_decompression_bomb_headers_are_rejected(self):
        for width, height in ((10_001, 1), (5000, 4001), (10_000, 10_000)):
            with self.subTest(width=width, height=height):
                result = await self.validator.validate(oversized_header(width, height))
                self.assertEqual(result.status, "invalid")
                self.assertEqual(result.error.code, "pixel_limit")

    async def test_boundary_dimensions_are_accepted(self):
        for size in ((10_000, 1), (5000, 4000)):
            with self.subTest(size=size):
                result = await self.validator.validate(encoded_image(size=size, mode="L"))
                self.assertEqual(result.status, "ok")
                self.assertEqual((result.width, result.height), size)

    async def test_apng_is_rejected(self):
        output = BytesIO()
        with Image.new("RGB", (16, 16), "red") as first, Image.new("RGB", (16, 16), "blue") as second:
            first.save(output, format="PNG", save_all=True, append_images=[second], duration=100, loop=0)
        result = await self.validator.validate(output.getvalue())
        self.assertEqual(result.error.code, "animated_image")

    async def test_exif_orientation_is_applied_to_reported_dimensions(self):
        exif = Image.Exif()
        exif[274] = 6
        result = await self.validator.validate(encoded_image("JPEG", size=(12, 8), exif=exif))
        self.assertEqual(result.status, "ok")
        self.assertEqual((result.width, result.height), (8, 12))

    async def test_grayscale_alpha_palette_and_cmyk_decode(self):
        fixtures = [encoded_image(mode=mode) for mode in ("L", "RGBA", "P")]
        fixtures.append(encoded_image("JPEG", mode="CMYK"))
        for data in fixtures:
            self.assertEqual((await self.validator.validate(data)).status, "ok")

    async def test_failed_download_and_missing_image_skip_worker(self):
        failure = Failure(stage="retrieval", code="download_timeout", message="Download timed out.", retryable=True)
        with patch.object(self.validator, "_run", new_callable=AsyncMock) as run:
            failed = await self.validator.validate_candidate(CandidateImage(status="failed", error=failure), None, scope="session-a")
            missing = await self.validator.validate_candidate(CandidateImage(status="unavailable"), None, scope="session-a")
        run.assert_not_awaited()
        self.assertEqual(failed.error, failure)
        self.assertEqual(failed.status, "unavailable")
        self.assertIsNone(failed.valid)
        self.assertEqual(missing.error.code, "image_unavailable")

    async def test_asset_handoff_uses_bytes_instead_of_mime_hint_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalAssetStore(directory)
            data = encoded_image()
            asset = store.put(data, scope="session-a", mime_type="image/jpeg")
            reference = CandidateImage(status="available", assetId=asset.id)
            result = await self.validator.validate_candidate(reference, store, scope="session-a")
            self.assertEqual(result.mime_type, "image/png")
            self.assertEqual(store.get(asset.id, scope="session-a")[1], data)
            denied = await self.validator.validate_candidate(reference, store, scope="session-b")
            self.assertEqual(denied.error.code, "asset_unavailable")
            self.assertIsNone(denied.valid)

    async def test_tampered_asset_is_storage_failure_not_invalid_image(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalAssetStore(directory)
            asset = store.put(b"original", scope="session-a")
            with sqlite3.connect(store.database) as db:
                db.execute("UPDATE assets SET data=? WHERE id=?", (b"tampered", asset.id))
            result = await self.validator.validate_candidate(CandidateImage(status="available", assetId=asset.id), store, scope="session-a")
            self.assertEqual(result.status, "error")
            self.assertIsNone(result.valid)
            self.assertEqual(result.error.code, "asset_integrity_failed")

    async def test_timeout_kills_worker_and_next_validation_succeeds(self):
        validator = IsolatedImageValidator(timeout_seconds=0.1)
        script = "import time; time.sleep(10)"
        with patch("candidate_processing.image_validation._worker_command", return_value=test_command(script)):
            result = await validator.validate(encoded_image())
        self.assertEqual(result.status, "error")
        self.assertIsNone(result.valid)
        self.assertEqual(result.error.code, "image_timeout")
        self.assertEqual((await self.validator.validate(encoded_image())).status, "ok")

    async def test_memory_budget_stops_worker(self):
        script = "import time; allocation=bytearray(64*1024**2); time.sleep(10)"
        validator = IsolatedImageValidator(timeout_seconds=5, memory_limit_bytes=32 * 1024**2)
        with patch("candidate_processing.image_validation._worker_command", return_value=test_command(script)):
            result = await validator.validate(encoded_image())
        self.assertEqual(result.error.code, "image_memory_limit")
        self.assertIsNone(result.valid)

    async def test_crash_returns_failure_then_real_worker_recovers(self):
        with patch("candidate_processing.image_validation._worker_command", return_value=test_command("import os; os._exit(3)")):
            failed = await self.validator.validate(encoded_image())
        self.assertEqual(failed.error.code, "image_worker_failed")
        self.assertEqual((await self.validator.validate(encoded_image())).status, "ok")

    async def test_malformed_and_inconsistent_worker_reports_are_rejected(self):
        reports = ["not JSON", '{"status":"ok"}', '{"status":"invalid","code":"invented"}',
                   '{"status":"ok","mimeType":"image/png","width":5000,"height":5000}']
        for report in reports:
            with self.subTest(report=report), patch("candidate_processing.image_validation._worker_command", return_value=test_command(f"print({report!r})")):
                result = await self.validator.validate(encoded_image())
                self.assertEqual(result.error.code, "invalid_worker_response")
                self.assertIsNone(result.valid)

    async def test_excessive_worker_output_is_bounded(self):
        script = "import sys,time; sys.stdout.write('x'*1000000); sys.stdout.flush(); time.sleep(10)"
        with patch("candidate_processing.image_validation._worker_command", return_value=test_command(script)):
            result = await self.validator.validate(encoded_image())
        self.assertEqual(result.error.code, "worker_output_limit")

    async def test_cancellation_reaps_worker_and_releases_lock(self):
        spawned = []
        original_spawn = asyncio.create_subprocess_exec
        started = asyncio.Event()

        async def record_spawn(*args, **kwargs):
            process = await original_spawn(*args, **kwargs)
            spawned.append(process)
            started.set()
            return process

        with patch("candidate_processing.image_validation._worker_command", return_value=test_command("import time; time.sleep(10)")), \
             patch("candidate_processing.image_validation.asyncio.create_subprocess_exec", side_effect=record_spawn):
            task = asyncio.create_task(self.validator.validate(encoded_image()))
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(spawned[0].returncode)
        self.assertFalse(psutil.pid_exists(spawned[0].pid))
        self.assertEqual((await self.validator.validate(encoded_image())).status, "ok")

    async def test_worker_does_not_inherit_provider_secrets_or_python_path(self):
        script = (
            "import os,sys,json; "
            "assert sys.flags.isolated; "
            "assert not any(key in os.environ for key in "
            "['TYPESAFE_API_KEY','AWS_SECRET_ACCESS_KEY','FACETRACE_API_TOKEN','PYTHONPATH','HTTP_PROXY']); "
            "print(json.dumps({'status':'ok','mimeType':'image/png','width':1,'height':1}))"
        )
        fake_env = {key: "test-value" for key in ("TYPESAFE_API_KEY", "AWS_SECRET_ACCESS_KEY", "FACETRACE_API_TOKEN", "PYTHONPATH", "HTTP_PROXY")}
        with patch.dict(os.environ, fake_env), patch("candidate_processing.image_validation._worker_command", return_value=test_command(script)):
            result = await self.validator.validate(encoded_image())
        self.assertEqual(result.status, "ok")

    async def test_start_and_monitor_failures_return_explicit_errors(self):
        with patch("candidate_processing.image_validation.asyncio.create_subprocess_exec", new_callable=AsyncMock, side_effect=OSError("private internal detail")):
            result = await self.validator.validate(encoded_image())
        self.assertEqual(result.error.code, "worker_start_failed")
        self.assertNotIn("private", result.error.message)
        with patch("candidate_processing.image_validation.psutil.Process", side_effect=psutil.AccessDenied()):
            result = await self.validator.validate(encoded_image())
        self.assertEqual(result.error.code, "worker_monitor_unavailable")

    async def test_invalid_configuration_and_nonbytes_are_rejected(self):
        for timeout in (0, float("inf"), True):
            with self.assertRaises(ValueError):
                IsolatedImageValidator(timeout_seconds=timeout)
        with self.assertRaises(ValueError):
            IsolatedImageValidator(memory_limit_bytes=0)
        with self.assertRaises(TypeError):
            await self.validator.validate(bytearray(b"image"))


class DecoderNormalizationTests(unittest.TestCase):
    def test_transparent_pixels_are_white_and_original_bytes_unchanged(self):
        output = BytesIO()
        with Image.new("RGBA", (2, 1), (255, 0, 0, 0)) as image:
            image.putpixel((1, 0), (0, 0, 255, 255))
            image.save(output, "PNG")
        original = output.getvalue()
        rgb, mime = decode_rgb(original)
        with rgb:
            self.assertEqual(rgb.mode, "RGB")
            self.assertEqual(rgb.getpixel((0, 0)), (255, 255, 255))
            self.assertEqual(rgb.getpixel((1, 0)), (0, 0, 255))
            self.assertEqual(rgb.info, {})
        self.assertEqual(mime, "image/png")
        self.assertEqual(original, output.getvalue())


if __name__ == "__main__":
    unittest.main()
