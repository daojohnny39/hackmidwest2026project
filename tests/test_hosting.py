"""Image hosting (spec 1.2, 0.5). S3 with a fake client: keys recorded before upload, plain
public URLs, the bucket check, cleanup, purge. The tunnel with a fake ssh: images served on loopback only, then stopped."""

import io
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from pipeline.artifacts import UPLOADS_FILE, purge_run, read_uploads
from pipeline.config import ConfigError, load_settings
from pipeline.hosting import (
    HostingError, S3ImageHost, TunnelImageHost, delete_object, host_for, image_type, require_hosting, search_copy,
)
from pipeline.search.serpapi_backend import build_serpapi_backend
from pipeline.trace import TRACE_FILE, tracing
from tests.support import OfflineTestCase, read_trace

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32


class FakeS3:
    def __init__(self, run_dir, *, version=None, fail_put=False):
        self.run_dir, self.version, self.fail_put = run_dir, version, fail_put
        self.calls = []
        self.objects = {}  # key -> body, for a fake public GET

    def put_object(self, **params):
        # The key must already be in uploads.json when the upload starts.
        recorded = [record.key for record in read_uploads(self.run_dir).objects]
        self.calls.append(("put", params["Key"], params["Key"] in recorded, params["ContentType"]))
        if self.fail_put:
            raise RuntimeError("AccessDenied https://faces.s3.amazonaws.com/?X-Amz-Signature=x")
        self.objects[params["Key"]] = params["Body"]
        return {"VersionId": self.version} if self.version else {}

    def delete_object(self, **params):
        self.calls.append(("delete", params["Key"], params.get("VersionId")))
        self.objects.pop(params["Key"], None)

    def public_get(self, url, timeout):
        """Stands in for an anonymous GET of the object's public URL."""
        return self.objects.get(url.removeprefix("https://faces.s3.us-east-1.amazonaws.com/"))


class HostingTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.runs = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.run_dir = self.runs / "session-0000abcd"
        self.run_dir.mkdir()

    def host(self, *, probe=None, **options):
        self.s3 = FakeS3(self.run_dir, **options)
        return S3ImageHost(bucket="faces", region="us-east-1", run_dir=self.run_dir, client=self.s3,
                           probe=probe or self.s3.public_get)

    def test_upload_records_first_and_hands_out_a_plain_public_url(self):
        url, record = self.host().upload(photo(orientation=6))
        # No query string: SerpApi passes the URL to Yandex unencoded, which cut presigned URLs short.
        self.assertEqual(url, f"https://faces.s3.us-east-1.amazonaws.com/{record.key}")
        self.assertRegex(record.key, r"^hackmidwest-faces/[0-9a-f]{32}\.jpg$")
        self.assertEqual(self.s3.calls, [("put", record.key, True, "image/jpeg")])
        # The public object is the small upright copy, with no EXIF.
        with Image.open(io.BytesIO(self.s3.objects[record.key])) as hosted:
            self.assertEqual((hosted.format, hosted.size), ("JPEG", (600, 800)))
            self.assertEqual(dict(hosted.getexif()), {})
        stored = (self.run_dir / UPLOADS_FILE).read_text()
        self.assertNotIn("://", stored)

    def test_keys_are_random_and_typed(self):
        host = self.host()
        keys = {host.upload(photo())[1].key for _ in range(3)} | {host.upload(photo(fmt="PNG"))[1].key}
        self.assertEqual(len(keys), 4)
        self.assertTrue(all(key.endswith(".jpg") for key in keys))
        self.assertEqual(len(read_uploads(self.run_dir).objects), 4)
        with self.assertRaises(HostingError):
            image_type(b"GIF89a")

    def test_start_checks_the_bucket_end_to_end(self):
        host = self.host()
        with tracing(self.run_dir, 1):
            host.start()
        [put, delete] = self.s3.calls
        self.assertTrue(put[1].startswith("hackmidwest-faces/") and put[1].endswith(".txt"))
        self.assertEqual(delete[:2], ("delete", put[1]))
        [record] = read_uploads(self.run_dir).objects
        self.assertTrue(record.deleted)
        self.assertEqual(self.s3.objects, {})
        self.assertNotIn(put[1], (self.run_dir / TRACE_FILE).read_text())

    def test_private_bucket_fails_start_and_still_deletes_the_probe(self):
        host = self.host(probe=lambda url, timeout: None)  # e.g. 403: no public-read bucket policy
        with tracing(self.run_dir, 1), self.assertRaises(HostingError) as caught:
            host.start()
        self.assertEqual(str(caught.exception), "bucket_not_public")
        self.assertEqual(self.s3.objects, {})
        [record] = read_uploads(self.run_dir).objects
        self.assertTrue(record.deleted)
        [failure] = [event for event in read_trace(self.run_dir) if event.get("event") == "bucket_not_public"]
        self.assertEqual(failure["bucket"], "faces")

    def test_backend_reports_a_bucket_that_fails_its_check_as_config(self):
        settings = load_settings(backend="serpapi", environ={"SERPAPI_KEY": "k", "IMAGE_HOST": "s3",
                                                             "IMAGE_BUCKET": "faces", "AWS_REGION": "us-east-1"},
                                 dotenv_path=None)
        capture = SimpleNamespace(capture_dir=str(self.run_dir))
        s3 = FakeS3(self.run_dir)
        with patch("pipeline.hosting.s3_client", return_value=s3), \
                patch("pipeline.hosting._probe", return_value=None):
            with self.assertRaisesRegex(ConfigError, "bucket_not_public.*s3:GetObject"):
                build_serpapi_backend(settings, capture, self.run_dir, refresh=False)
        self.assertEqual(s3.objects, {})

    def test_delete_marks_deleted_and_handles_versions(self):
        host = self.host(version="v7")
        _, record = host.upload(photo())
        host.delete(record)
        self.assertEqual(self.s3.calls[-1], ("delete", record.key, "v7"))
        [stored] = read_uploads(self.run_dir).objects
        self.assertTrue(stored.deleted)
        self.assertEqual(stored.version_id, "v7")

    def test_failed_upload_stays_listed_for_purge(self):
        with self.assertRaises(HostingError) as caught:
            self.host(fail_put=True).upload(photo())
        self.assertEqual(str(caught.exception), "hosting_error")
        [record] = read_uploads(self.run_dir).objects
        self.assertFalse(record.deleted)
        deleted = []
        report = purge_run(self.runs, self.run_dir, delete_remote=lambda r: delete_object(r, client=self.s3) or deleted.append(r.key))
        self.assertTrue(report.run_deleted)
        self.assertEqual(deleted, [record.key])

    def test_missing_bucket_is_a_config_error(self):
        settings = load_settings(backend="mock", environ={"IMAGE_HOST": "s3"}, dotenv_path=None)
        with self.assertRaisesRegex(ConfigError, "IMAGE_BUCKET"):
            host_for(settings, self.run_dir)


TUNNEL = "https://0f1e2d3c4b5a69.lhr.life"
# Stands in for `ssh -R ... nokey@localhost.run`: records its arguments, then prints like localhost.run (or fails).
FAKE_SSH = """import sys, time
with open({calls!r}, "a") as stream:
    stream.write(" ".join(sys.argv[1:]) + "\\n")
if {mode!r} == "fail":
    print("nokey@localhost.run: Permission denied (publickey).", file=sys.stderr, flush=True)
    sys.exit(255)
print("Welcome to localhost.run! See https://localhost.run/docs/", flush=True)
print("{name}.lhr.life tunneled with tls termination, {tunnel}", flush=True)
time.sleep(60)
"""


def photo(size=(1600, 1200), fmt="JPEG", mode="RGB", orientation=None):
    """A real image with EXIF (camera model, optional orientation) in the given format."""
    exif = Image.Exif()
    exif[0x0110] = "TestCam"  # Model
    if orientation:
        exif[0x0112] = orientation
    output = io.BytesIO()
    Image.new(mode, size, (200, 120, 40, 255)[:len(mode)]).save(output, fmt, exif=exif)
    return output.getvalue()


def local_get(host, url, timeout=5.0):
    """GET a tunnel URL from the host's loopback server; returns (status, content type, body)."""
    local = url.replace(TUNNEL, f"http://127.0.0.1:{host._server.server_address[1]}")
    try:
        with urllib.request.urlopen(local, timeout=timeout) as response:
            return response.status, response.headers["Content-Type"], response.read()
    except urllib.error.HTTPError as error:
        return error.code, None, b""


class TunnelHostingTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.calls = self.folder / "calls.txt"

    def fake_ssh(self, mode="ok"):
        body = self.folder / "ssh.py"
        body.write_text(FAKE_SSH.format(calls=str(self.calls), mode=mode, tunnel=TUNNEL,
                                        name=TUNNEL.removeprefix("https://").split(".")[0]))
        script = self.folder / "ssh"
        # A shell launcher, since a shebang line can't hold the venv's path (it has a space).
        script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{body}" "$@"\n')
        script.chmod(0o755)
        return str(script)

    def host(self, mode="ok", probe=None, **options):
        host = TunnelImageHost(ssh=self.fake_ssh(mode), **options)
        host.probe = probe or (lambda url, timeout: local_get(host, url, timeout)[2])
        self.addCleanup(host.close)
        return host

    def test_serves_each_image_at_a_random_path_until_deleted(self):
        host = self.host()
        original = photo()
        url, record = host.upload(original)
        other, _ = host.upload(photo(fmt="PNG", mode="RGBA"))
        self.assertTrue(url.startswith(TUNNEL + "/") and url.endswith(".jpg") and other.endswith(".jpg"))
        self.assertNotEqual(url, other)
        self.assertEqual(local_get(host, url), (200, "image/jpeg", search_copy(original)))
        self.assertEqual(local_get(host, other)[:2], (200, "image/jpeg"))
        self.assertEqual(local_get(host, TUNNEL + "/")[0], 404)
        self.assertEqual(local_get(host, url + "x")[0], 404)
        host.delete(record)
        self.assertTrue(record.deleted)
        self.assertEqual(local_get(host, url)[0], 404)
        # One tunnel for the whole run, pointed at the loopback server, never prompting or offering a key.
        port = host._server.server_address[1]
        [call] = self.calls.read_text().splitlines()
        self.assertTrue(call.endswith(f"-R 80:127.0.0.1:{port} nokey@localhost.run"), call)
        for option in ("BatchMode=yes", "PubkeyAuthentication=no", "StrictHostKeyChecking=accept-new"):
            self.assertIn(f"-o {option}", call)

    def test_close_stops_the_tunnel_and_server(self):
        host = self.host()
        url, _ = host.upload(photo())
        process, port = host._process, host._server.server_address[1]
        host.close()
        self.assertIsNotNone(process.poll())
        with self.assertRaises(OSError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
        host.close()  # idempotent

    def test_failed_start_is_a_code_and_not_retried(self):
        host = self.host(mode="fail")
        for _ in range(2):
            with self.assertRaises(HostingError) as caught:
                host.upload(photo())
            self.assertEqual(str(caught.exception), "tunnel_error")
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_unreachable_tunnel_hands_out_no_url(self):
        host = self.host(probe=lambda url, timeout: None, probe_timeout=0.2)
        started = time.monotonic()
        with self.assertRaises(HostingError) as caught:
            host.upload(photo())
        self.assertEqual(str(caught.exception), "tunnel_unreachable")
        self.assertLess(time.monotonic() - started, 10)
        self.assertIsNone(host._process)  # stopped

    def test_trace_keeps_ssh_output_of_a_failed_tunnel_without_its_url(self):
        traces = self.folder / "traces"
        traces.mkdir()
        with tracing(traces, 1):
            # One at a time: each fake ssh rewrites the same script.
            for mode, options in (("fail", {}), ("ok", {"probe": lambda url, timeout: None, "probe_timeout": 0.2})):
                with self.assertRaises(HostingError):
                    self.host(mode, **options).upload(photo())
        refused, unreachable = [line for line in read_trace(traces) if line["event"] == "tunnel_failed"]
        self.assertIn("nokey@localhost.run: Permission denied (publickey).", refused["ssh_output"])
        self.assertIn("[REDACTED].lhr.life tunneled with tls termination, https://[REDACTED].lhr.life",
                      unreachable["ssh_output"])
        self.assertNotIn(TUNNEL.removeprefix("https://"), (traces / TRACE_FILE).read_text())

    def test_search_copy_is_small_upright_jpeg_without_metadata(self):
        # Orientation 6 means "rotate 90° clockwise to view", so 1600x1200 is shown as 1200x1600.
        with Image.open(io.BytesIO(search_copy(photo(orientation=6)))) as copy:
            self.assertEqual((copy.format, copy.size), ("JPEG", (600, 800)))
            self.assertEqual(dict(copy.getexif()), {})
        with Image.open(io.BytesIO(search_copy(photo((300, 200), fmt="PNG", mode="RGBA")))) as copy:
            self.assertEqual((copy.format, copy.size, copy.mode), ("JPEG", (300, 200), "RGB"))  # never enlarged
        for data in (JPEG, b"not an image"):
            with self.assertRaises(HostingError) as caught:
                search_copy(data)
            self.assertEqual(str(caught.exception), "unsupported_image")

    def test_require_hosting_by_image_host(self):
        settings = load_settings(backend="mock", environ={}, dotenv_path=None)
        self.assertEqual(settings.image_host, "tunnel")
        with patch("pipeline.hosting.find_ssh", return_value=None):
            with self.assertRaisesRegex(ConfigError, "ssh"):
                require_hosting(settings)
        with patch("pipeline.hosting.find_ssh", return_value=self.fake_ssh()):
            host = host_for(settings, self.folder)
        self.assertIsInstance(host, TunnelImageHost)
        self.assertIsNone(host._process)  # nothing starts until start() or the first upload
        self.assertFalse((self.folder / UPLOADS_FILE).exists())

    def test_backend_opens_the_tunnel_first_or_fails_as_config(self):
        settings = load_settings(backend="serpapi", environ={"SERPAPI_KEY": "k"}, dotenv_path=None)
        capture = SimpleNamespace(capture_dir=str(self.folder))
        with patch("pipeline.hosting.find_ssh", return_value=self.fake_ssh("fail")):
            with self.assertRaisesRegex(ConfigError, "tunnel_error"):
                build_serpapi_backend(settings, capture, self.folder, refresh=False)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
