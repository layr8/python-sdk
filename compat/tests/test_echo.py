"""Unit tests for the echo scenario logic."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import websockets.asyncio.server

from scenarios.echo import ECHO_TYPE, ECHO_RESPONSE_TYPE, run_receiver, run_sender
from scenarios.types import ScenarioContext, SenderContext


PING_TYPE = "https://didcomm.org/trust-ping/2.0/ping"
PING_RESPONSE_TYPE = "https://didcomm.org/trust-ping/2.0/ping-response"


class MockPhoenixServer:
    """Minimal Phoenix Channel V2 mock server for compat scenario tests."""

    def __init__(self) -> None:
        self._server: websockets.asyncio.server.Server | None = None
        self._connections: list[websockets.asyncio.server.ServerConnection] = []
        self._received: list[dict[str, Any]] = []
        self.port = 0
        self._assigned_dids: dict[int, str] = {}
        self._did_counter = 0
        self._pending_dispatches: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        self._server = await websockets.asyncio.server.serve(
            self._handler, "127.0.0.1", 0,
        )
        sock = list(self._server.sockets)[0]
        self.port = sock.getsockname()[1]

    async def _handler(self, ws: websockets.asyncio.server.ServerConnection) -> None:
        self._connections.append(ws)
        conn_id = id(ws)

        try:
            async for raw in ws:
                arr = json.loads(raw)
                join_ref, ref, topic, event, payload = arr

                if event == "phx_join":
                    topic_did = topic.removeprefix("plugins:")
                    if topic_did:
                        assigned_did = topic_did
                    else:
                        self._did_counter += 1
                        assigned_did = f"did:web:node:agent-{self._did_counter}"
                    self._assigned_dids[conn_id] = assigned_did

                    await ws.send(json.dumps([
                        ref, ref, topic, "phx_reply",
                        {
                            "status": "ok",
                            "response": {
                                "did": assigned_did,
                                "capabilities": ["reply_protocol/1"],
                            },
                        },
                    ]))
                elif event == "message":
                    self._received.append({"event": event, "payload": payload, "ws": ws})
                    if ref:
                        await ws.send(json.dumps([
                            None, ref, topic, "phx_reply",
                            {"status": "ok", "response": {}},
                        ]))
                    sender_did = self._assigned_dids.get(conn_id, "")
                    msg_id = payload.get("id", "") if isinstance(payload, dict) else ""
                    # Route message to all OTHER connections
                    for other_ws in self._connections:
                        if other_ws is not ws and other_ws.state.name == "OPEN":
                            recipient_did = self._assigned_dids.get(id(other_ws), "")
                            # Track for trust-ping fallback
                            if msg_id:
                                self._pending_dispatches[f"{recipient_did}:{msg_id}"] = {
                                    "sender_did": sender_did,
                                    "plaintext": payload,
                                }
                            await other_ws.send(json.dumps([
                                None, None, topic, "message",
                                {
                                    "context": {
                                        "recipient": recipient_did,
                                        "authorized": True,
                                        "sender_credentials": [],
                                    },
                                    "plaintext": payload,
                                },
                            ]))
                elif event == "dispatch_reply":
                    if ref:
                        await ws.send(json.dumps([
                            None, ref, topic, "phx_reply",
                            {"status": "ok", "response": {}},
                        ]))
                    # Trust-ping fallback on PASS
                    status = payload.get("status", "") if isinstance(payload, dict) else ""
                    message_id = payload.get("message_id", "") if isinstance(payload, dict) else ""
                    recipient_did = self._assigned_dids.get(conn_id, "")
                    key = f"{recipient_did}:{message_id}"
                    pending = self._pending_dispatches.pop(key, None)
                    if status == "pass" and pending:
                        pt = pending["plaintext"]
                        if isinstance(pt, dict) and pt.get("type") == PING_TYPE:
                            body = pt.get("body", {})
                            if isinstance(body, dict) and body.get("responseRequested"):
                                sender = next(
                                    (c for c in self._connections
                                     if self._assigned_dids.get(id(c)) == pending["sender_did"]
                                     and c.state.name == "OPEN"),
                                    None,
                                )
                                if sender:
                                    thid = pt.get("thid") or pt.get("id", "")
                                    sender_topic = f"plugins:{pending['sender_did']}"
                                    await sender.send(json.dumps([
                                        None, None, sender_topic, "message",
                                        {
                                            "context": {
                                                "recipient": pending["sender_did"],
                                                "authorized": True,
                                                "sender_credentials": [],
                                            },
                                            "plaintext": {
                                                "id": f"mock-{message_id}",
                                                "type": PING_RESPONSE_TYPE,
                                                "from": recipient_did,
                                                "to": [pending["sender_did"]],
                                                "thid": thid,
                                                "body": {},
                                            },
                                        },
                                    ]))
                else:
                    if ref:
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


class TestEchoScenario:
    async def test_echo_passes(self, mock_server: MockPhoenixServer) -> None:
        """Receiver echoes back, sender gets pass."""
        receiver_ctx = ScenarioContext(
            node_url=mock_server.ws_url,
            api_key="test-key",
            test_id="test-echo-1",
            timeout=5.0,
        )

        # Start receiver in background
        receiver_task = asyncio.create_task(run_receiver(receiver_ctx, on_ready=lambda did: None))
        await asyncio.sleep(0.3)  # let receiver connect

        sender_ctx = SenderContext(
            node_url=mock_server.ws_url,
            api_key="test-key",
            test_id="test-echo-1",
            timeout=5.0,
            receiver_did="did:web:node:agent-1",
        )

        result = await run_sender(sender_ctx)
        assert result.status == "pass"
        assert result.scenario == "echo"
        assert result.duration_ms >= 0

        receiver_task.cancel()
        try:
            await receiver_task
        except asyncio.CancelledError:
            pass


class TestEchoOnReady:
    async def test_on_ready_called_with_did(self, mock_server: MockPhoenixServer) -> None:
        receiver_ctx = ScenarioContext(
            node_url=mock_server.ws_url,
            api_key="test-key",
            test_id="test-ready-1",
            timeout=5.0,
        )
        ready_dids: list[str] = []
        receiver_task = asyncio.create_task(
            run_receiver(receiver_ctx, on_ready=lambda did: ready_dids.append(did))
        )
        await asyncio.sleep(0.3)
        assert len(ready_dids) == 1
        assert ready_dids[0].startswith("did:web:")
        receiver_task.cancel()
        try:
            await receiver_task
        except asyncio.CancelledError:
            pass


class TestEchoExplicitDID:
    async def test_explicit_did_from_join_topic(self, mock_server: MockPhoenixServer) -> None:
        explicit_did = "did:web:node:explicit-test"
        ctx = ScenarioContext(
            node_url=mock_server.ws_url,
            api_key="test-key",
            test_id="test-did-1",
            timeout=5.0,
            agent_did=explicit_did,
        )
        ready_dids: list[str] = []
        receiver_task = asyncio.create_task(
            run_receiver(ctx, on_ready=lambda did: ready_dids.append(did))
        )
        await asyncio.sleep(0.3)
        receiver_task.cancel()
        try:
            await receiver_task
        except asyncio.CancelledError:
            pass
        assert ready_dids == [explicit_did]