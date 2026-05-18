"""Unit tests for the disconnected scenario logic."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import websockets.asyncio.server

from scenarios.disconnected import run_receiver, run_sender
from scenarios.types import ScenarioContext, SenderContext
from tests.test_echo import MockPhoenixServer


class ProtocolCaptureMock:
    """Mock server that captures and validates join payload_types."""

    def __init__(self) -> None:
        self._server: websockets.asyncio.server.Server | None = None
        self.port = 0
        self.join_payloads: list[dict[str, Any]] = []

    async def start(self) -> None:
        self._server = await websockets.asyncio.server.serve(
            self._handler, "127.0.0.1", 0,
        )
        sock = list(self._server.sockets)[0]
        self.port = sock.getsockname()[1]

    async def _handler(self, ws: websockets.asyncio.server.ServerConnection) -> None:
        try:
            async for raw in ws:
                arr = json.loads(raw)
                join_ref, ref, topic, event, payload = arr
                if event == "phx_join":
                    self.join_payloads.append(payload)
                    protocols = payload.get("payload_types", [])
                    if not protocols:
                        await ws.send(json.dumps([
                            ref, ref, topic, "phx_reply",
                            {"status": "error", "response": {"reason": "e.join.plugin.protocol.missing: No protocol specified"}},
                        ]))
                    else:
                        did = topic.removeprefix("plugins:") or "did:web:node:test"
                        await ws.send(json.dumps([
                            ref, ref, topic, "phx_reply",
                            {"status": "ok", "response": {"did": did, "capabilities": ["reply_protocol/1"]}},
                        ]))
                elif ref:
                    await ws.send(json.dumps([
                        None, ref, topic, "phx_reply",
                        {"status": "ok", "response": {}},
                    ]))
        except Exception:
            pass

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/plugin_socket/websocket"

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


@pytest.fixture
async def protocol_server():
    server = ProtocolCaptureMock()
    await server.start()
    yield server
    await server.close()


class TestDisconnectedScenario:
    async def test_disconnected_passes(self, mock_server: MockPhoenixServer) -> None:
        """Sending to a non-existent DID times out gracefully → pass."""
        sender_ctx = SenderContext(
            node_url=mock_server.ws_url,
            api_key="test-key",
            test_id="test-disconnected-1",
            timeout=1.0,
            receiver_did="did:web:nobody:nonexistent",
        )

        result = await run_sender(sender_ctx)
        assert result.status == "pass"
        assert result.scenario == "disconnected"

    async def test_receiver_connects_and_signals_ready(
        self, mock_server: MockPhoenixServer
    ) -> None:
        """Receiver connects, emits ready with DID, and waits."""
        ctx = ScenarioContext(
            node_url=mock_server.ws_url,
            api_key="test-key",
            test_id="test-disconnected-recv",
            timeout=5.0,
            agent_did="did:web:node:disconnected-recv",
        )

        ready_did: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        def on_ready(did: str) -> None:
            if not ready_did.done():
                ready_did.set_result(did)

        task = asyncio.create_task(run_receiver(ctx, on_ready=on_ready))

        did = await asyncio.wait_for(ready_did, timeout=3.0)
        assert did  # non-empty DID

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_receiver_specifies_protocol_on_join(
        self, protocol_server: ProtocolCaptureMock
    ) -> None:
        """Receiver must specify a protocol — cloud-node rejects empty."""
        ctx = ScenarioContext(
            node_url=protocol_server.ws_url,
            api_key="test-key",
            test_id="test-disconnected-proto",
            timeout=5.0,
            agent_did="did:web:node:disconnected-proto",
        )

        ready_did: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        def on_ready(did: str) -> None:
            if not ready_did.done():
                ready_did.set_result(did)

        task = asyncio.create_task(run_receiver(ctx, on_ready=on_ready))

        did = await asyncio.wait_for(ready_did, timeout=3.0)
        assert did

        assert len(protocol_server.join_payloads) == 1
        protocols = protocol_server.join_payloads[0]["payload_types"]
        assert len(protocols) > 0, "receiver must specify at least one protocol"

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass