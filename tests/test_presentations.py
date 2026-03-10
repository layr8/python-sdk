"""Tests for presentation APIs on the Layr8 Client."""

from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp import web

from layr8.client import Client
from layr8.presentations import VerifiedPresentation
from layr8.rest import RestClient


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


class TestSignPresentation:
    async def test_default_options(self) -> None:
        captured: dict[str, Any] = {}

        async def handler(request: web.Request) -> web.Response:
            assert request.path == "/api/v1/presentations/sign"
            captured["body"] = await request.json()
            return web.json_response(
                {"signed_presentation": "eyJhbGciOiJFZERTQSJ9.vp.sig"}
            )

        app = web.Application()
        app.router.add_post("/api/v1/presentations/sign", handler)
        server = await _start_server(app)

        try:
            client = _make_client(server.port)
            try:
                signed = await client.sign_presentation(["jwt1", "jwt2"])

                assert signed == "eyJhbGciOiJFZERTQSJ9.vp.sig"
                assert captured["body"]["holder_did"] == AGENT_DID
                assert captured["body"]["format"] == "compact_jwt"
                assert captured["body"]["credentials"] == ["jwt1", "jwt2"]
                assert "nonce" not in captured["body"]
            finally:
                await client._rest.close()
        finally:
            await server.close()

    async def test_with_options(self) -> None:
        captured: dict[str, Any] = {}

        async def handler(request: web.Request) -> web.Response:
            captured["body"] = await request.json()
            return web.json_response({"signed_presentation": "{}"})

        app = web.Application()
        app.router.add_post("/api/v1/presentations/sign", handler)
        server = await _start_server(app)

        try:
            client = _make_client(server.port)
            try:
                await client.sign_presentation(
                    ["jwt1"],
                    holder_did="did:web:custom.localhost:holder",
                    format="json",
                    nonce="challenge-123",
                )

                assert captured["body"]["holder_did"] == "did:web:custom.localhost:holder"
                assert captured["body"]["format"] == "json"
                assert captured["body"]["nonce"] == "challenge-123"
            finally:
                await client._rest.close()
        finally:
            await server.close()


class TestVerifyPresentation:
    async def test_default_verifier(self) -> None:
        captured: dict[str, Any] = {}

        async def handler(request: web.Request) -> web.Response:
            assert request.path == "/api/v1/presentations/verify"
            captured["body"] = await request.json()
            return web.json_response({
                "presentation": {
                    "type": ["VerifiablePresentation"],
                    "verifiableCredential": ["jwt1"],
                },
                "headers": {"alg": "EdDSA"},
            })

        app = web.Application()
        app.router.add_post("/api/v1/presentations/verify", handler)
        server = await _start_server(app)

        try:
            client = _make_client(server.port)
            try:
                result = await client.verify_presentation("eyJ.vp.sig")

                assert captured["body"]["verifier_did"] == AGENT_DID
                assert result.headers["alg"] == "EdDSA"
            finally:
                await client._rest.close()
        finally:
            await server.close()

    async def test_with_verifier_did(self) -> None:
        captured: dict[str, Any] = {}

        async def handler(request: web.Request) -> web.Response:
            captured["body"] = await request.json()
            return web.json_response({"presentation": {}, "headers": {}})

        app = web.Application()
        app.router.add_post("/api/v1/presentations/verify", handler)
        server = await _start_server(app)

        try:
            client = _make_client(server.port)
            try:
                await client.verify_presentation(
                    "jwt", verifier_did="did:web:custom.localhost:verifier"
                )
                assert captured["body"]["verifier_did"] == "did:web:custom.localhost:verifier"
            finally:
                await client._rest.close()
        finally:
            await server.close()
