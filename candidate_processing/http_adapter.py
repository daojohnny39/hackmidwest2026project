"""Optional local FastAPI adapter. Run via the explicit environment factory."""

import asyncio
import hmac
import os
import sqlite3

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile

from .contracts import BatchResult, CandidateBatch
from .image_analysis import IsolatedImageAnalyzer
from .image_validation import IsolatedImageValidator
from .pipeline import analyze_candidates
from .storage import AssetIntegrityError, AssetNotFound, LocalAssetStore, MAX_IMAGE_BYTES, StorageFull

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_MULTIPART_BYTES = MAX_IMAGE_BYTES + 64 * 1024
PRIVATE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


class RequestBoundary:
    """Authenticate before parsing/spooling, then bound even chunked bodies."""

    def __init__(self, app, *, token: str):
        self.app, self.token = app, token.encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        auth = [value for key, value in headers if key.lower() == b"authorization"]
        valid = False
        if len(auth) == 1:
            scheme, separator, supplied = auth[0].partition(b" ")
            valid = bool(separator) and scheme.lower() == b"bearer" and hmac.compare_digest(supplied, self.token)
        if not valid:
            response = JSONResponse({"detail": "Authentication required."}, status_code=401,
                                    headers={**PRIVATE_HEADERS, "WWW-Authenticate": "Bearer"})
            return await response(scope, receive, send)
        maximum = MAX_MULTIPART_BYTES if scope["path"] == "/api/v1/assets" and scope["method"] == "POST" else MAX_JSON_BYTES
        try:
            lengths = [int(value) for key, value in headers if key.lower() == b"content-length"]
            if any(length < 0 for length in lengths) or len(lengths) > 1:
                raise ValueError
        except ValueError:
            return await JSONResponse({"detail": "Invalid content length."}, status_code=400)(scope, receive, send)
        if lengths and lengths[0] > maximum:
            return await JSONResponse({"detail": "Request body too large."}, status_code=413)(scope, receive, send)
        body = bytearray()
        try:
            async with asyncio.timeout(30):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > maximum:
                        return await JSONResponse({"detail": "Request body too large."}, status_code=413)(scope, receive, send)
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await JSONResponse({"detail": "Request body timed out."}, status_code=408)(scope, receive, send)
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        async def private_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (key.lower().encode(), value.encode()) for key, value in PRIVATE_HEADERS.items()
                ]
            await send(message)

        await self.app(scope, bounded_receive, private_send)


def create_app(*, store: LocalAssetStore, scope: str, api_token: str,
               analyzer=None, provider=None, validator=None, deadline_seconds=120.0) -> FastAPI:
    """One backend-configured principal/scope. Member 1 owns production auth.

    Injected providers remain caller-owned. Without one, each analyze request
    owns a fresh Jev client. The analyzer serializes worker inference across
    concurrent requests; waiting for it consumes the combined batch deadline.
    """
    LocalAssetStore._validate_scope(scope)
    if not isinstance(api_token, str) or len(api_token.strip()) < 32:
        raise ValueError("Configure a bearer token containing at least 32 characters")
    analyzer = analyzer if analyzer is not None else IsolatedImageAnalyzer()
    validator = validator if validator is not None else IsolatedImageValidator()
    app = FastAPI(title="Member 3 candidate processing", version="1", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestBoundary, token=api_token)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _error):
        # FastAPI's default 422 includes submitted values; don't echo metadata.
        return JSONResponse({"detail": "Request does not satisfy the version 1 contract."}, status_code=422)

    @app.exception_handler(AssetNotFound)
    async def missing_asset(_request, _error):
        return JSONResponse({"detail": "Asset not found."}, status_code=404)

    @app.exception_handler(StorageFull)
    async def quota_exceeded(_request, _error):
        return JSONResponse({"detail": "Asset storage quota reached."}, status_code=507)

    async def storage_failed(_request, _error):
        return JSONResponse({"detail": "Asset storage is unavailable."}, status_code=503)

    for exception in (AssetIntegrityError, OSError, sqlite3.Error):
        app.add_exception_handler(exception, storage_failed)

    @app.post("/api/v1/assets", status_code=201)
    async def upload_asset(request: Request):
        async with request.form(max_files=1, max_fields=0) as form:
            if list(form.keys()) != ["file"] or not isinstance(form["file"], UploadFile):
                raise HTTPException(422, "Supply one multipart file named file.")
            data = await form["file"].read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "Image exceeds the 10 MiB byte limit.")
        validation = await validator.validate(data)
        if validation.status != "ok":
            raise HTTPException(422 if validation.status == "invalid" else 503, {
                "code": validation.error.code, "message": validation.error.message,
            })
        asset = await asyncio.to_thread(store.put, data, scope=scope, mime_type=validation.mime_type)
        return {"assetId": asset.id, "byteLength": asset.size, "contentSha256": asset.sha256,
                "mimeType": asset.mime_type, "expiresAt": asset.expires_at}

    @app.get("/api/v1/assets/{asset_id}")
    async def get_asset(asset_id: str):
        asset, data = await asyncio.to_thread(store.get, asset_id, scope=scope)
        # Backend originals may have only a MIME hint. Attachment + nosniff
        # prevents serving an arbitrary caller-supplied payload as active content.
        return Response(data, media_type=asset.mime_type, headers={
            "Content-Disposition": f'attachment; filename="{asset.id}"',
        })

    @app.delete("/api/v1/assets/{asset_id}", status_code=204)
    async def delete_asset(asset_id: str):
        await asyncio.to_thread(store.delete, asset_id, scope=scope)
        return Response(status_code=204)

    @app.post("/api/v1/candidates/analyze", response_model=BatchResult)
    async def analyze_batch(request: CandidateBatch):
        return await analyze_candidates(request, store=store, scope=scope, analyzer=analyzer,
                                        provider=provider, deadline_seconds=deadline_seconds)

    return app


def create_app_from_env() -> FastAPI:
    """uvicorn candidate_processing.http_adapter:create_app_from_env --factory"""
    token = os.environ.get("FACETRACE_API_TOKEN", "")
    scope = os.environ.get("FACETRACE_ASSET_SCOPE", "")
    # Fail before creating storage when authentication is unconfigured.
    LocalAssetStore._validate_scope(scope)
    if len(token.strip()) < 32:
        raise ValueError("Configure FACETRACE_API_TOKEN with at least 32 characters")
    return create_app(store=LocalAssetStore(), scope=scope, api_token=token)
