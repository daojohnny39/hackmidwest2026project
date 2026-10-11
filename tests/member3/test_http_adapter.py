"""Exercise the real ASGI routes, parser, validation and authorized asset store."""

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from candidate_processing.contracts import BatchResult
from candidate_processing.http_adapter import MAX_JSON_BYTES, create_app, create_app_from_env
from candidate_processing.storage import LocalAssetStore, MAX_IMAGE_BYTES
from tests.member3.test_classification import MockJev, candidate
from tests.member3.test_pipeline import image_bytes, image_report

TOKEN = "synthetic-local-token-for-tests-123456"


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalAssetStore(self.temp.name)
        self.analyzer = AsyncMock()
        self.analyzer.analyze.return_value = image_report()
        self.provider = MockJev()
        self.app = create_app(store=self.store, scope="owner", api_token=TOKEN, analyzer=self.analyzer, provider=self.provider)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test",
                                        headers={"Authorization": f"Bearer {TOKEN}"})
        self.addAsyncCleanup(self.client.aclose)

    async def upload(self, data=None, **settings):
        return await self.client.post("/api/v1/assets", files={"file": ("untrusted-name.png", image_bytes() if data is None else data, "image/png")}, **settings)

    async def test_upload_analyze_download_delete_roundtrip(self):
        upload = await self.upload()
        self.assertEqual(upload.status_code, 201, upload.text)
        asset = upload.json()
        self.assertEqual(asset["mimeType"], "image/jpeg")  # Verified content beats extension/header.
        self.assertEqual(asset["byteLength"], len(image_bytes()))
        item = candidate(image={"status": "available", "assetId": asset["assetId"]})
        response = await self.client.post("/api/v1/candidates/analyze", json={
            "schemaVersion": 1, "requestId": "http-test", "candidates": [item.model_dump(mode="json", by_alias=True)],
        })
        self.assertEqual(response.status_code, 200, response.text)
        result = BatchResult.model_validate(response.json())
        self.assertEqual(result.candidates[0].classification.relevance, "high")
        response = await self.client.get(f'/api/v1/assets/{asset["assetId"]}')
        self.assertEqual(response.content, image_bytes())
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))
        deleted = await self.client.delete(f'/api/v1/assets/{asset["assetId"]}')
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual((await self.client.get(f'/api/v1/assets/{asset["assetId"]}')).status_code, 404)

    async def test_unauthorized_rejected_before_reading_body(self):
        messages = []
        async def receive():
            self.fail("Unauthorized request body must not be read")
        async def send(message):
            messages.append(message)
        await self.app({"type": "http", "method": "POST", "path": "/api/v1/assets", "headers": []}, receive, send)
        self.assertEqual(messages[0]["status"], 401)

    async def test_wrong_token_and_duplicate_authorization_fail(self):
        for headers in ({"Authorization": "Bearer wrong"},
                        [("Authorization", f"Bearer {TOKEN}"), ("Authorization", "Bearer wrong")]):
            response = await self.client.get("/api/v1/assets/missing", headers=headers)
            self.assertEqual(response.status_code, 401)

    async def test_scope_cannot_be_selected_by_input_or_query(self):
        hidden = self.store.put(image_bytes(), scope="someone-else")
        response = await self.client.get(f"/api/v1/assets/{hidden.id}?scope=someone-else")
        self.assertEqual(response.status_code, 404)
        response = await self.client.delete(f"/api/v1/assets/{hidden.id}?scope=someone-else")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.store.get(hidden.id, scope="someone-else")[1], image_bytes())

    async def test_corrupt_upload_rejected_and_no_asset_stored(self):
        response = await self.upload(b"invalid image")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "unsupported_image_format")
        with self.store._transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 0)

    async def test_unknown_json_fields_and_duplicate_ids_rejected_without_work(self):
        item = candidate(image={"status": "unavailable"}).model_dump(mode="json", by_alias=True)
        for extra in ({"scope": "PRIVATE"}, {"candidates": [item, item]}):
            payload = {"schemaVersion": 1, "requestId": "http-test", "candidates": [item], **extra}
            response = await self.client.post("/api/v1/candidates/analyze", json=payload)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("PRIVATE", response.text)
        self.assertEqual(self.provider.calls, [])
        self.analyzer.analyze.assert_not_awaited()

    async def test_declared_and_chunked_body_limits(self):
        response = await self.client.post("/api/v1/candidates/analyze", content=b"{}", headers={"Content-Length": str(MAX_JSON_BYTES + 1)})
        self.assertEqual(response.status_code, 413)
        async def chunks():
            for _ in range(3):
                yield b"x" * (MAX_JSON_BYTES // 2)
        response = await self.client.post("/api/v1/candidates/analyze", content=chunks())
        self.assertEqual(response.status_code, 413)

    async def test_multipart_rejects_extra_files_and_fields(self):
        response = await self.client.post("/api/v1/assets", files=[("file", ("one", image_bytes())), ("file", ("two", image_bytes()))])
        self.assertEqual(response.status_code, 400)
        response = await self.client.post("/api/v1/assets", files={"file": ("one", image_bytes())}, data={"scope": "another"})
        self.assertEqual(response.status_code, 400)

    async def test_oversize_file_is_rejected_before_decode(self):
        response = await self.upload(b"x" * (MAX_IMAGE_BYTES + 1))
        self.assertEqual(response.status_code, 413)

    async def test_storage_quota_returns_explicit_failure(self):
        self.store.max_bytes = 1
        response = await self.upload()
        self.assertEqual(response.status_code, 507)

    async def test_configuration_fails_closed(self):
        with patch.dict("os.environ", {"FACETRACE_API_TOKEN": "", "FACETRACE_ASSET_SCOPE": ""}):
            with self.assertRaises(ValueError):
                create_app_from_env()
        with self.assertRaises(ValueError):
            create_app(store=self.store, scope="owner", api_token="")


if __name__ == "__main__":
    unittest.main()
