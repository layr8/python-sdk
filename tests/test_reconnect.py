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
    """Minimal Phoenix Channel V2 mock server for reconnect tests.

    It applies the one rule of Phoenix.Socket that decides whether a leave
    takes effect (phoenix 1.7, ``Phoenix.Socket.handle_in/4``): a phx_leave
    reaches the channel only when its join_ref equals the ref the topic was
    joined with on the same connection. Any other leave is dropped without a
    reply and the channel, and the node's binding of the DID, keeps running.
    Accepted leaves land in ``left_topics``, dropped ones in
    ``ignored_leaves``, so a test asserts the node would act on the leave, not
    only that one was written.
    """

    def __init__(self) -> None:
        self._server: websockets.asyncio.server.Server | None = None
        self._client_ws: websockets.asyncio.server.ServerConnection | None = None
        self.port = 0
        self.left_topics: list[str] = []
        self.ignored_leaves: list[str] = []

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
        # Joins are per connection: a new socket knows none of the old one's.
        joins: dict[str, str | None] = {}
        try:
            async for raw in ws:
                arr = json.loads(raw)
                event = arr[3]
                if event == "phx_join":
                    joins[arr[2]] = arr[0]
                elif event == "phx_leave":
                    if arr[2] in joins and joins[arr[2]] == arr[0]:
                        del joins[arr[2]]
                        self.left_topics.append(arr[2])
                    else:
                        self.ignored_leaves.append(arr[2])
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

    async def test_close_sends_leave_the_node_acts_on(self, mock_server: MockPhoenixServer) -> None:
        ch = _make_channel(mock_server.port)
        await ch.connect(["test-protocol"])
        # Traffic after the join moves the ref counter past the join ref, so a
        # leave carrying its own ref (or none) would not match.
        await ch.send_fire_and_forget("message", {})

        await ch.close()

        await _wait_for_leave(mock_server, "plugins:did:web:test")

    async def test_close_after_reconnect_sends_leave_the_node_acts_on(
        self, mock_server: MockPhoenixServer
    ) -> None:
        # Note: the join ref is "1" on every connection (_dial resets the
        # counter and the join is the first frame), so this cannot tell a
        # stale ref from a fresh one. It pins that the leave reaches the
        # second connection with a ref that connection accepts.
        reconnect_event = asyncio.Event()
        ch = _make_channel(mock_server.port, on_reconnect=reconnect_event.set)
        await ch.connect(["test-protocol"])

        await mock_server.force_close_client()
        await asyncio.wait_for(reconnect_event.wait(), timeout=5)
        await ch.send_fire_and_forget("message", {})

        await ch.close()

        await _wait_for_leave(mock_server, "plugins:did:web:test")


async def _wait_for_leave(server: MockPhoenixServer, topic: str) -> None:
    for _ in range(300):
        assert not server.ignored_leaves, (
            f"server ignored phx_leave for {server.ignored_leaves} "
            f"(join_ref did not match the join); accepted: {server.left_topics}"
        )
        if topic in server.left_topics:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"server never accepted phx_leave for {topic!r}; accepted: {server.left_topics}"
    )
