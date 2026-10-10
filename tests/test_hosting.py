"""S3 image hosting with a fake client (spec 1.2, 0.5): keys recorded before upload, cleanup, purge."""

import tempfile
from pathlib import Path

from pipeline.artifacts import UPLOADS_FILE, purge_run, read_uploads
from pipeline.config import ConfigError, load_settings
from pipeline.hosting import HostingError, S3ImageHost, delete_object, host_for, image_type
from tests.support import OfflineTestCase

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class FakeS3:
    def __init__(self, run_dir, *, version=None, fail_put=False):
        self.run_dir, self.version, self.fail_put = run_dir, version, fail_put
        self.calls = []

    def put_object(self, **params):
        # The key must already be in uploads.json when the upload starts.
        recorded = [record.key for record in read_uploads(self.run_dir).objects]
        self.calls.append(("put", params["Key"], params["Key"] in recorded, params["ContentType"]))
        if self.fail_put:
            raise RuntimeError("AccessDenied https://faces.s3.amazonaws.com/?X-Amz-Signature=x")
        return {"VersionId": self.version} if self.version else {}

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.calls.append(("presign", Params["Key"], ExpiresIn))
        return f"https://{Params['Bucket']}.s3.amazonaws.com/{Params['Key']}?X-Amz-Signature=abc"

    def delete_object(self, **params):
        self.calls.append(("delete", params["Key"], params.get("VersionId")))


class HostingTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.runs = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.run_dir = self.runs / "session-0000abcd"
        self.run_dir.mkdir()

    def host(self, **options):
        self.s3 = FakeS3(self.run_dir, **options)
        return S3ImageHost(bucket="faces", region="us-east-1", run_dir=self.run_dir, client=self.s3)

    def test_upload_records_first_and_presigns_for_ten_minutes(self):
        url, record = self.host().upload(JPEG)
        self.assertTrue(url.startswith("https://faces.s3.amazonaws.com/hackmidwest-faces/"))
        self.assertTrue(record.key.endswith(".jpg"))
        self.assertEqual(self.s3.calls[0], ("put", record.key, True, "image/jpeg"))
        self.assertEqual(self.s3.calls[1], ("presign", record.key, 600))
        stored = (self.run_dir / UPLOADS_FILE).read_text()
        self.assertNotIn("X-Amz", stored)
        self.assertNotIn("://", stored)

    def test_keys_are_random_and_typed(self):
        host = self.host()
        keys = {host.upload(JPEG)[1].key for _ in range(3)} | {host.upload(PNG)[1].key}
        self.assertEqual(len(keys), 4)
        self.assertEqual(len(read_uploads(self.run_dir).objects), 4)
        with self.assertRaises(HostingError):
            image_type(b"GIF89a")

    def test_delete_marks_deleted_and_handles_versions(self):
        host = self.host(version="v7")
        _, record = host.upload(JPEG)
        host.delete(record)
        self.assertEqual(self.s3.calls[-1], ("delete", record.key, "v7"))
        [stored] = read_uploads(self.run_dir).objects
        self.assertTrue(stored.deleted)
        self.assertEqual(stored.version_id, "v7")

    def test_failed_upload_stays_listed_for_purge(self):
        with self.assertRaises(HostingError) as caught:
            self.host(fail_put=True).upload(JPEG)
        self.assertEqual(str(caught.exception), "hosting_error")
        [record] = read_uploads(self.run_dir).objects
        self.assertFalse(record.deleted)
        deleted = []
        report = purge_run(self.runs, self.run_dir, delete_remote=lambda r: delete_object(r, client=self.s3) or deleted.append(r.key))
        self.assertTrue(report.run_deleted)
        self.assertEqual(deleted, [record.key])

    def test_missing_bucket_is_a_config_error(self):
        settings = load_settings(backend="mock", environ={}, dotenv_path=None)
        with self.assertRaisesRegex(ConfigError, "IMAGE_BUCKET"):
            host_for(settings, self.run_dir)
