"""Tests for layr8.rest — URL conversion and RestClient."""

from __future__ import annotations

import pytest
from aiohttp import web

from layr8.rest import RESTError, RestClient, rest_url_from_websocket


class TestRestURLFromWebSocket:
    def test_ws_with_path(self) -> None:
        assert (
            rest_url_from_websocket("ws://alice-test.localhost/plugin_socket/websocket")
            == "http://alice-test.localhost"
        )

    def test_wss_with_path(self) -> None:
        assert (
            rest_url_from_websocket("wss://alice-test.localhost/plugin_socket/websocket")
            == "https://alice-test.localhost"
        )

    def test_ws_with_port_and_path(self) -> None:
        assert (
            rest_url_from_websocket("ws://localhost:4000/plugin_socket/websocket")
            == "http://localhost:4000"
        )

    def test_wss_with_port_and_path(self) -> None:
        assert (
            rest_url_from_websocket("wss://mynode.layr8.cloud:443/plugin_socket/websocket")
            == "https://mynode.layr8.cloud:443"
        )

    def test_ws_no_path(self) -> None:
        assert (
            rest_url_from_websocket("ws://localhost:4000")
            == "http://localhost:4000"
        )


class TestRestClient:
    async def test_post_sends_json_with_api_key(self) -> None:
        """POST sets Content-Type and x-api-key headers."""
        captured: dict = {}

        async def handler(request: web.Request) -> web.Response:
            captured["headers"] = dict(request.headers)
            captured["body"] = await request.json()
            return web.json_response({"result": "ok"})

        app = web.Application()
        app.router.add_post("/test", handler)
        server = await _start_server(app)

        try:
            client = RestClient(f"http://127.0.0.1:{server.port}", "my-api-key")
            try:
                result = await client.post("/test", {"key": "value"})
                assert result == {"result": "ok"}
                assert captured["headers"]["Content-Type"] == "application/json"
                assert captured["headers"]["x-api-key"] == "my-api-key"
                assert captured["body"] == {"key": "value"}
            finally:
                await client.close()
        finally:
            await server.close()

    async def test_get_sends_api_key(self) -> None:
        """GET sets x-api-key header."""
        captured: dict = {}

        async def handler(request: web.Request) -> web.Response:
            captured["headers"] = dict(request.headers)
            return web.json_response({"data": [1, 2, 3]})

        app = web.Application()
        app.router.add_get("/items", handler)
        server = await _start_server(app)

        try:
            client = RestClient(f"http://127.0.0.1:{server.port}", "test-key")
            try:
                result = await client.get("/items")
                assert result == {"data": [1, 2, 3]}
                assert captured["headers"]["x-api-key"] == "test-key"
            finally:
                await client.close()
        finally:
            await server.close()

    async def test_error_response_parsed(self) -> None:
        """HTTP 4xx responses raise RESTError with parsed message."""

        async def handler(request: web.Request) -> web.Response:
            return web.json_response(
                {"error": "Not found"}, status=404
            )

        app = web.Application()
        app.router.add_get("/missing", handler)
        server = await _start_server(app)

        try:
            client = RestClient(f"http://127.0.0.1:{server.port}", "key")
            try:
                with pytest.raises(RESTError) as exc_info:
                    await client.get("/missing")
                assert exc_info.value.status_code == 404
                assert exc_info.value.message == "Not found"
            finally:
                await client.close()
        finally:
            await server.close()

    async def test_error_response_raw_body(self) -> None:
        """Non-JSON error bodies are returned as raw text."""

        async def handler(request: web.Request) -> web.Response:
            return web.Response(text="Internal Server Error", status=500)

        app = web.Application()
        app.router.add_get("/fail", handler)
        server = await _start_server(app)

        try:
            client = RestClient(f"http://127.0.0.1:{server.port}", "key")
            try:
                with pytest.raises(RESTError) as exc_info:
                    await client.get("/fail")
                assert exc_info.value.status_code == 500
                assert "Internal Server Error" in exc_info.value.message
            finally:
                await client.close()
        finally:
            await server.close()


class _MockServer:
    """Thin wrapper to manage an aiohttp test server on a random port."""

    def __init__(self, runner: web.AppRunner, port: int) -> None:
        self.runner = runner
        self.port = port

    async def close(self) -> None:
        await self.runner.cleanup()


async def _start_server(app: web.Application) -> _MockServer:
    """Start an aiohttp app on a random port and return a _MockServer."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    # Get the actual bound port
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return _MockServer(runner, port)
