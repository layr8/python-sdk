"""Tests for PhoenixChannel auto-reconnect behaviour."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
import websockets
import websockets.asyncio.server

from layr8.channel import PhoenixChannel
from layr8.errors import NotConnectedError


class MockPhoenixServer:
    """Minimal Phoenix Channel V2 mock server for reconnect tests."""

    def __init__(self) -> None:
        self._server: websockets.asyncio.server.Server | None = None
        self._client_ws: websockets.asyncio.server.ServerConnection | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await websockets.asyncio.server.serve(
            self._handler,
            "127.0.0.1",
            0,
        )
        sock = list(self._server.sockets)[0]
        self.port = sock.getsockname()[1]

    async def _handler(
        self, ws: websockets.asyncio.server.ServerConnection
    ) -> None:
        self._client_ws = ws
        try:
            async for raw in ws:
                arr = json.loads(raw)
                event = arr[3]
                if event == "phx_join":
                    reply = [arr[0], arr[1], arr[2], "phx_reply", {"status": "ok", "response": {"did": "did:web:node:test"}}]
                    await ws.send(json.dumps(reply))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def force_close_client(self) -> None:
        """Force-close the client WebSocket from the server side."""
        if self._client_ws:
            await self._client_ws.close()
            self._client_ws = None

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()


@pytest.fixture
async def mock_server():
    server = MockPhoenixServer()
    await server.start()
    yield server
    await server.close()


def _make_channel(port: int, on_message: Any = None, on_disconnect: Any = None, on_reconnect: Any = None) -> PhoenixChannel:
    return PhoenixChannel(
        f"ws://127.0.0.1:{port}/plugin_socket/websocket",
        "test-api-key",
        "did:web:test",
        on_message=on_message or (lambda _: None),
        on_disconnect=on_disconnect,
        on_reconnect=on_reconnect,
    )


class TestReconnect:
    async def test_reconnect_after_drop(self, mock_server: MockPhoenixServer) -> None:
        """After server drops connection, on_disconnect then on_reconnect fire."""
        disconnect_event = asyncio.Event()
        reconnect_event = asyncio.Event()

        def on_disconnect(exc: Exception) -> None:
            disconnect_event.set()

        def on_reconnect() -> None:
            reconnect_event.set()

        ch = _make_channel(mock_server.port, on_disconnect=on_disconnect, on_reconnect=on_reconnect)
        await ch.connect(["test-protocol"])

        # Force the server to drop the connection
        await mock_server.force_close_client()

        # Wait for disconnect callback
        await asyncio.wait_for(disconnect_event.wait(), timeout=3)

        # Wait for reconnect callback (backoff starts at 1s)
        await asyncio.wait_for(reconnect_event.wait(), timeout=5)

        assert not ch._reconnecting
        await ch.close()

    async def test_fail_fast_during_reconnect(self, mock_server: MockPhoenixServer) -> None:
        """send() raises NotConnectedError while reconnecting."""
        disconnect_event = asyncio.Event()

        def on_disconnect(exc: Exception) -> None:
            disconnect_event.set()

        ch = _make_channel(mock_server.port, on_disconnect=on_disconnect)
        await ch.connect(["test-protocol"])

        # Force disconnect
        await mock_server.force_close_client()
        await asyncio.wait_for(disconnect_event.wait(), timeout=3)

        # Give the reconnect loop a moment to start
        await asyncio.sleep(0.1)

        # Should raise NotConnectedError because we're reconnecting
        with pytest.raises(NotConnectedError):
            await ch.send("message", {"body": "test"})

        await ch.close()

    async def test_close_stops_reconnect(self, mock_server: MockPhoenixServer) -> None:
        """Calling close() during reconnect stops the reconnect loop."""
        disconnect_event = asyncio.Event()

        def on_disconnect(exc: Exception) -> None:
            disconnect_event.set()

        reconnect_called = False

        def on_reconnect() -> None:
            nonlocal reconnect_called
            reconnect_called = True

        ch = _make_channel(mock_server.port, on_disconnect=on_disconnect, on_reconnect=on_reconnect)
        await ch.connect(["test-protocol"])

        # Force disconnect
        await mock_server.force_close_client()
        await asyncio.wait_for(disconnect_event.wait(), timeout=3)

        # Close immediately (before reconnect can succeed)
        await ch.close()

        assert ch._closed
        assert not ch._reconnecting

        # Wait a bit to confirm on_reconnect was NOT called
        await asyncio.sleep(0.5)
        assert not reconnect_called
