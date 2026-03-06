"""Tests for credential APIs on the Layr8 Client."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web

from layr8.client import Client
from layr8.credentials import Credential
from layr8.rest import RESTError, RestClient


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

AGENT_DID = "did:web:test.localhost:test-agent"


class _MockServer:
    """Manages an aiohttp test server on a random port."""

    def __init__(self, runner: web.AppRunner, port: int) -> None:
        self.runner = runner
        self.port = port

    async def close(self) -> None:
        await self.runner.cleanup()


async def _start_server(app: web.Application) -> _MockServer:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return _MockServer(runner, port)


def _make_client(port: int) -> Client:
    """Create a minimal Client with only the REST client wired up."""
    client = Client.__new__(Client)
    client._rest = RestClient(f"http://127.0.0.1:{port}", "test-api-key")
    client._agent_did = AGENT_DID
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSignCredential:
    async def test_default_options(self) -> None:
        captured: dict[str, Any] = {}

        async def handler(request: web.Request) -> web.Response:
            assert request.path == "/api/v1/credentials/sign"
            assert request.method == "POST"
            assert request.headers.get("x-api-key") == "test-api-key"
            captured["body"] = await request.json()
            return web.json_response(
                {"signed_credential": "eyJhbGciOiJFZERTQSJ9.test.signature"}
            )

        app = web.Application()
        app.router.add_post("/api/v1/credentials/sign", handler)
        server = await _start_server(app)

        try:
            client = _make_client(server.port)
            try:
                cred = Credential(
                    id="urn:uuid:test-123",
                    issuer="did:web:test.localhost:test-agent",
                    credential_subject={"id": "customer-abc", "org": "testorg"},
                )
                signed = await client.sign_credential(cred)

                assert signed == "eyJhbGciOiJFZERTQSJ9.test.signature"
                assert captured["body"]["issuer_did"] == AGENT_DID
                assert captured["body"]["format"] == "compact_jwt"
            finally:
                await client._rest.close()
        finally:
            await server.close()

    async def test_with_options(self) -> None:
        captured: dict[str, Any] = {}

        async def handler(request: web.Request) -> web.Response:
            captured["body"] = await request.json()
            return web.json_response({"signed_credential": "{}"})

        app = web.Application()
        app.router.add_post("/api/v1/credentials/sign", handler)
        server = await _start_server(app)

        try:
            client = _make_client(server.port)
            try:
                cred = Credential(credential_subject={"test": True})
                await client.sign_credential(
                    cred,
                    issuer_did="did:web:other.localhost:other-agent",
                    format="json",
                )

                assert captured["body"]["issuer_did"] == "did:web:other.localhost:other-agent"
                assert captured["body"]["format"] == "json"
            finally:
                await client._rest.close()
        finally:
            await server.close()


class TestVerifyCredential:
    async def test_default_verifier(self) -> None:
        captured: dict[str, Any] = {}

        async def handler(request: web.Request) -> web.Response:
            assert request.path == "/api/v1/credentials/verify"
            captured["body"] = await request.json()
            return web.json_response({
                "credential": {
                    "id": "urn:uuid:test-123",
                    "credentialSubject": {"id": "customer-abc", "org": "testorg"},
                },
                "headers": {"alg": "EdDSA"},
            })

        app = web.Application()
        app.router.add_post("/api/v1/credentials/verify", handler)
        server = await _start_server(app)

        try:
            client = _make_client(server.port)
            try:
                result = await client.verify_credential("eyJhbGciOiJFZERTQSJ9.test.sig")

                assert captured["body"]["verifier_did"] == AGENT_DID
                assert result.credential["id"] == "urn:uuid:test-123"
                assert result.headers["alg"] == "EdDSA"
            finally:
                await client._rest.close()
        finally:
            await server.close()

    async def test_with_verifier_did(self) -> None:
        captured: dict[str, Any] = {}

        async def handler(request: web.Request) -> web.Response:
            captured["body"] = await request.json()
            return web.json_response({"credential": {}, "headers": {}})

        app = web.Application()
        app.router.add_post("/api/v1/credentials/verify", handler)
        server = await _start_server(app)

        try:
            client = _make_client(server.port)
            try:
                await client.verify_credential(
                    "jwt", verifier_did="did:web:custom.localhost:agent"
                )
                assert captured["body"]["verifier_did"] == "did:web:custom.localhost:agent"
            finally:
                await client._rest.close()
        finally:
            await server.close()


class TestStoreCredential:
    async def test_default_holder(self) -> None:
        captured: dict[str, Any] = {}

        async def handler(request: web.Request) -> web.Response:
            assert request.path == "/api/v1/credentials"
            assert request.method == "POST"
            captured["body"] = await request.json()
            body = captured["body"]
            return web.json_response(
                {
                    "id": "urn:uuid:stored-123",
                    "holder_did": body["holder_did"],
                    "credential_jwt": body["credential_jwt"],
                },
                status=201,
            )

        app = web.Application()
        app.router.add_post("/api/v1/credentials", handler)
        server = await _start_server(app)

        try:
            client = _make_client(server.port)
            try:
                result = await client.store_credential("eyJ0ZXN0.jwt.here")

                assert result.id == "urn:uuid:stored-123"
                assert captured["body"]["holder_did"] == AGENT_DID
            finally:
                await client._rest.close()
        finally:
            await server.close()

    async def test_with_meta(self) -> None:
        from datetime import datetime, timezone

        valid_until = datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        captured: dict[str, Any] = {}

        async def handler(request: web.Request) -> web.Response:
            captured["body"] = await request.json()
            return web.json_response(
                {"id": "urn:uuid:stored-456", "holder_did": captured["body"]["holder_did"]},
                status=201,
            )

        app = web.Application()
        app.router.add_post("/api/v1/credentials", handler)
        server = await _start_server(app)

        try:
            client = _make_client(server.port)
            try:
                await client.store_credential(
                    "jwt",
                    holder_did="did:web:custom.localhost:holder",
                    issuer_did="did:web:issuer.localhost:agent",
                    valid_until=valid_until,
                )

                assert captured["body"]["holder_did"] == "did:web:custom.localhost:holder"
                assert captured["body"]["issuer_did"] == "did:web:issuer.localhost:agent"
                assert captured["body"]["valid_until"] is not None
            finally:
                await client._rest.close()
        finally:
            await server.close()


class TestListCredentials:
    async def test_default_holder(self) -> None:
        captured: dict[str, Any] = {}

        async def handler(request: web.Request) -> web.Response:
            assert request.method == "GET"
            captured["holder_did"] = request.query.get("holder_did")
            return web.json_response({
                "credentials": [
                    {"id": "cred-1", "holder_did": AGENT_DID, "credential_jwt": "jwt1"},
                    {"id": "cred-2", "holder_did": AGENT_DID, "credential_jwt": "jwt2"},
                ]
            })

        app = web.Application()
        app.router.add_get("/api/v1/credentials", handler)
        server = await _start_server(app)

        try:
            client = _make_client(server.port)
            try:
                creds = await client.list_credentials()

                assert len(creds) == 2
                assert captured["holder_did"] == AGENT_DID
            finally:
                await client._rest.close()
        finally:
            await server.close()


class TestGetCredential:
    async def test_by_id(self) -> None:
        captured: dict[str, Any] = {}

        async def handler(request: web.Request) -> web.Response:
            captured["path"] = request.path_qs
            assert request.method == "GET"
            return web.json_response({
                "id": "urn:uuid:test-123",
                "holder_did": AGENT_DID,
                "credential_jwt": "jwt-data",
            })

        app = web.Application()
        app.router.add_get("/api/v1/credentials/{cred_id:.+}", handler)
        server = await _start_server(app)

        try:
            client = _make_client(server.port)
            try:
                cred = await client.get_credential("urn:uuid:test-123")

                assert cred.id == "urn:uuid:test-123"
                # The credential ID is percent-encoded in the URL path
                assert "urn" in captured["path"]
                assert "test-123" in captured["path"]
            finally:
                await client._rest.close()
        finally:
            await server.close()


class TestSignCredentialError:
    async def test_rest_error(self) -> None:
        async def handler(request: web.Request) -> web.Response:
            return web.json_response(
                {"error": "No assertion key found for issuer DID"}, status=404
            )

        app = web.Application()
        app.router.add_post("/api/v1/credentials/sign", handler)
        server = await _start_server(app)

        try:
            client = _make_client(server.port)
            try:
                with pytest.raises(RESTError) as exc_info:
                    await client.sign_credential(
                        Credential(credential_subject={"test": True})
                    )
                assert exc_info.value.status_code == 404
                assert "No assertion key found" in exc_info.value.message
            finally:
                await client._rest.close()
        finally:
            await server.close()
