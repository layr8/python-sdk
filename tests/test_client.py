"""Tests for layr8.client — uses an in-process mock Phoenix WebSocket server."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import websockets
import websockets.asyncio.server

from layr8 import Client, Config, Message, ProblemReportError


class MockPhoenixServer:
    """Minimal Phoenix Channel V2 mock server for testing."""

    def __init__(self) -> None:
        self._server: websockets.asyncio.server.Server | None = None
        self._client_ws: websockets.asyncio.server.ServerConnection | None = None
        self._received: list[dict[str, Any]] = []
        self.on_msg: Any = None
        self.port = 0

    async def start(self) -> None:
        self._server = await websockets.asyncio.server.serve(
            self._handler,
            "127.0.0.1",
            0,
        )
        # Get the bound port
        sock = list(self._server.sockets)[0]
        self.port = sock.getsockname()[1]

    async def _handler(
        self, ws: websockets.asyncio.server.ServerConnection
    ) -> None:
        self._client_ws = ws
        try:
            async for raw in ws:
                arr = json.loads(raw)
                msg = {
                    "join_ref": arr[0],
                    "ref": arr[1],
                    "topic": arr[2],
                    "event": arr[3],
                    "payload": arr[4],
                }
                self._received.append({"event": msg["event"], "payload": msg["payload"]})
                if self.on_msg:
                    self.on_msg(msg)
        except websockets.exceptions.ConnectionClosed:
            pass

    async def send_to_client(
        self,
        join_ref: str | None,
        ref: str | None,
        topic: str,
        event: str,
        payload: Any,
    ) -> None:
        if self._client_ws:
            await self._client_ws.send(
                json.dumps([join_ref, ref, topic, event, payload])
            )

    def get_received(self) -> list[dict[str, Any]]:
        return list(self._received)

    def clear_received(self) -> None:
        self._received.clear()

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()


@pytest.fixture
async def mock_server():
    """Create and start a mock Phoenix WebSocket server."""
    server = MockPhoenixServer()
    await server.start()

    # Default: auto-reply to phx_join
    def default_handler(msg: dict[str, Any]) -> None:
        if msg["event"] == "phx_join":
            asyncio.ensure_future(
                server.send_to_client(
                    msg["ref"],
                    msg["ref"],
                    msg["topic"],
                    "phx_reply",
                    {"status": "ok", "response": {"did": "did:web:node:test"}},
                )
            )

    server.on_msg = default_handler
    yield server
    await server.close()


def ws_url(server: MockPhoenixServer) -> str:
    return f"ws://127.0.0.1:{server.port}/plugin_socket/websocket"


class TestClient:
    async def test_creates_with_valid_config(self) -> None:
        client = Client(Config(
            node_url="ws://localhost:4000/plugin_socket/websocket",
            api_key="test-key",
            agent_did="did:web:test",
        ))
        assert client is not None

    async def test_raises_when_node_url_missing(self) -> None:
        with pytest.raises(Exception, match="node_url is required"):
            Client(Config(api_key="test-key"))

    async def test_raises_when_api_key_missing(self) -> None:
        with pytest.raises(Exception, match="api_key is required"):
            Client(Config(node_url="ws://localhost:4000"))

    async def test_connects_and_closes(self, mock_server: MockPhoenixServer) -> None:
        client = Client(Config(
            node_url=ws_url(mock_server),
            api_key="test-key",
            agent_did="did:web:test",
        ))
        await client.connect()
        await client.close()

    async def test_assigns_did_from_node(self, mock_server: MockPhoenixServer) -> None:
        client = Client(Config(
            node_url=ws_url(mock_server),
            api_key="test-key",
            agent_did="",
        ))
        await client.connect()
        assert client.did == "did:web:node:test"
        await client.close()

    async def test_handle_before_connect(self) -> None:
        client = Client(Config(
            node_url="ws://localhost:4000",
            api_key="test-key",
            agent_did="did:web:test",
        ))

        @client.handle("https://layr8.io/protocols/echo/1.0/request")
        async def handler(msg: Message) -> None:
            return None

        # Should not raise

    async def test_handle_after_connect_raises(
        self, mock_server: MockPhoenixServer
    ) -> None:
        client = Client(Config(
            node_url=ws_url(mock_server),
            api_key="test-key",
            agent_did="did:web:test",
        ))

        @client.handle("https://layr8.io/protocols/echo/1.0/request")
        async def handler(msg: Message) -> None:
            return None

        await client.connect()
        try:
            with pytest.raises(Exception, match="already connected"):
                client.handle(
                    "https://layr8.io/protocols/echo/1.0/response",
                    handler,
                )
        finally:
            await client.close()

    async def test_send(self, mock_server: MockPhoenixServer) -> None:
        client = Client(Config(
            node_url=ws_url(mock_server),
            api_key="test-key",
            agent_did="did:web:alice",
        ))

        @client.handle("https://layr8.io/protocols/echo/1.0/request")
        async def handler(msg: Message) -> None:
            return None

        await client.connect()

        await client.send(Message(
            type="https://didcomm.org/basicmessage/2.0/message",
            to=["did:web:bob"],
            body={"content": "hello"},
        ))

        await asyncio.sleep(0.2)
        received = mock_server.get_received()
        msg_events = [r for r in received if r["event"] == "message"]
        assert len(msg_events) > 0

        await client.close()

    async def test_send_not_connected_raises(self) -> None:
        client = Client(Config(
            node_url="ws://localhost:4000",
            api_key="test-key",
            agent_did="did:web:test",
        ))
        with pytest.raises(Exception, match="not connected"):
            await client.send(Message(type="test", to=["did:web:bob"]))

    async def test_request_response_correlation(
        self, mock_server: MockPhoenixServer
    ) -> None:
        def handler(msg: dict[str, Any]) -> None:
            if msg["event"] == "phx_join":
                asyncio.ensure_future(
                    mock_server.send_to_client(
                        msg["ref"], msg["ref"], msg["topic"],
                        "phx_reply", {"status": "ok", "response": {}},
                    )
                )
            elif msg["event"] == "message":
                payload = msg["payload"]
                asyncio.ensure_future(
                    mock_server.send_to_client(
                        None, None, "plugins:did:web:alice", "message",
                        {
                            "plaintext": {
                                "id": "resp-1",
                                "type": "https://layr8.io/protocols/echo/1.0/response",
                                "from": "did:web:bob",
                                "to": [payload.get("from", "")],
                                "thid": payload.get("thid", ""),
                                "body": {"echo": "hello"},
                            }
                        },
                    )
                )

        mock_server.on_msg = handler

        client = Client(Config(
            node_url=ws_url(mock_server),
            api_key="test-key",
            agent_did="did:web:alice",
        ))

        @client.handle("https://layr8.io/protocols/echo/1.0/request")
        async def echo(msg: Message) -> None:
            return None

        await client.connect()

        resp = await client.request(
            Message(
                type="https://layr8.io/protocols/echo/1.0/request",
                to=["did:web:bob"],
                body={"message": "hello"},
            ),
            timeout=5.0,
        )

        assert resp.type == "https://layr8.io/protocols/echo/1.0/response"
        body = resp.unmarshal_body()
        assert body["echo"] == "hello"

        await client.close()

    async def test_request_timeout(self, mock_server: MockPhoenixServer) -> None:
        client = Client(Config(
            node_url=ws_url(mock_server),
            api_key="test-key",
            agent_did="did:web:alice",
        ))

        @client.handle("https://layr8.io/protocols/echo/1.0/request")
        async def handler(msg: Message) -> None:
            return None

        await client.connect()

        with pytest.raises(asyncio.TimeoutError):
            await client.request(
                Message(
                    type="https://layr8.io/protocols/echo/1.0/request",
                    to=["did:web:nobody"],
                    body={"message": "hello"},
                ),
                timeout=0.2,
            )

        await client.close()

    async def test_request_problem_report(
        self, mock_server: MockPhoenixServer
    ) -> None:
        def handler(msg: dict[str, Any]) -> None:
            if msg["event"] == "phx_join":
                asyncio.ensure_future(
                    mock_server.send_to_client(
                        msg["ref"], msg["ref"], msg["topic"],
                        "phx_reply", {"status": "ok", "response": {}},
                    )
                )
            elif msg["event"] == "message":
                payload = msg["payload"]
                asyncio.ensure_future(
                    mock_server.send_to_client(
                        None, None, "plugins:did:web:alice", "message",
                        {
                            "plaintext": {
                                "id": "err-1",
                                "type": "https://didcomm.org/report-problem/2.0/problem-report",
                                "from": "did:web:bob",
                                "thid": payload.get("thid", ""),
                                "body": {
                                    "code": "e.p.xfer.cant-process",
                                    "comment": "database unavailable",
                                },
                            }
                        },
                    )
                )

        mock_server.on_msg = handler

        client = Client(Config(
            node_url=ws_url(mock_server),
            api_key="test-key",
            agent_did="did:web:alice",
        ))

        @client.handle("https://layr8.io/protocols/echo/1.0/request")
        async def echo(msg: Message) -> None:
            return None

        await client.connect()

        with pytest.raises(ProblemReportError) as exc_info:
            await client.request(
                Message(
                    type="https://layr8.io/protocols/echo/1.0/request",
                    to=["did:web:bob"],
                    body={"message": "hello"},
                ),
                timeout=5.0,
            )

        assert exc_info.value.code == "e.p.xfer.cant-process"
        await client.close()

    async def test_inbound_handler_dispatched(
        self, mock_server: MockPhoenixServer
    ) -> None:
        client = Client(Config(
            node_url=ws_url(mock_server),
            api_key="test-key",
            agent_did="did:web:alice",
        ))

        received_msg: asyncio.Future[Message] = asyncio.get_running_loop().create_future()

        @client.handle("https://didcomm.org/basicmessage/2.0/message")
        async def handler(msg: Message) -> None:
            if not received_msg.done():
                received_msg.set_result(msg)
            return None

        await client.connect()

        await mock_server.send_to_client(
            None, None, "plugin:lobby", "message",
            {
                "context": {
                    "recipient": "did:web:alice",
                    "authorized": True,
                    "sender_credentials": [
                        {"credential_subject": {"id": "did:web:bob", "name": "Bob"}}
                    ],
                },
                "plaintext": {
                    "id": "inbound-1",
                    "type": "https://didcomm.org/basicmessage/2.0/message",
                    "from": "did:web:bob",
                    "to": ["did:web:alice"],
                    "body": {"content": "hello alice"},
                },
            },
        )

        msg = await asyncio.wait_for(received_msg, timeout=2)
        assert msg.from_ == "did:web:bob"
        assert msg.context is not None
        assert msg.context.authorized is True
        assert msg.context.sender_credentials[0].name == "Bob"
        body = msg.unmarshal_body()
        assert body["content"] == "hello alice"

        # Verify ack was sent
        await asyncio.sleep(0.2)
        received = mock_server.get_received()
        assert any(r["event"] == "ack" for r in received)

        await client.close()

    async def test_handler_error_sends_problem_report(
        self, mock_server: MockPhoenixServer
    ) -> None:
        client = Client(Config(
            node_url=ws_url(mock_server),
            api_key="test-key",
            agent_did="did:web:alice",
        ))

        @client.handle("https://layr8.io/protocols/echo/1.0/request")
        async def handler(msg: Message) -> Message:
            raise RuntimeError("something went wrong")

        await client.connect()

        await mock_server.send_to_client(
            None, None, "plugin:lobby", "message",
            {
                "plaintext": {
                    "id": "req-1",
                    "type": "https://layr8.io/protocols/echo/1.0/request",
                    "from": "did:web:bob",
                    "to": ["did:web:alice"],
                    "body": {"message": "ping"},
                },
            },
        )

        await asyncio.sleep(0.5)
        received = mock_server.get_received()
        reports = [
            r for r in received
            if r["event"] == "message"
            and isinstance(r["payload"], dict)
            and r["payload"].get("type") == "https://didcomm.org/report-problem/2.0/problem-report"
        ]
        assert len(reports) == 1

        await client.close()

    async def test_join_rejected_includes_reason(self, mock_server: MockPhoenixServer) -> None:
        def handler(msg: dict[str, Any]) -> None:
            if msg["event"] == "phx_join":
                asyncio.ensure_future(
                    mock_server.send_to_client(
                        msg["ref"],
                        msg["ref"],
                        msg["topic"],
                        "phx_reply",
                        {
                            "status": "error",
                            "response": {
                                "reason": "e.connect.plugin.failed: protocols_already_bound",
                            },
                        },
                    )
                )

        mock_server.on_msg = handler

        client = Client(Config(
            node_url=ws_url(mock_server),
            api_key="test-key",
            agent_did="did:web:test",
        ))

        @client.handle("https://layr8.io/protocols/echo/1.0/request")
        async def echo(msg: Message) -> None:
            return None

        with pytest.raises(Exception, match="protocols_already_bound"):
            await client.connect()

    async def test_async_context_manager(
        self, mock_server: MockPhoenixServer
    ) -> None:
        client = Client(Config(
            node_url=ws_url(mock_server),
            api_key="test-key",
            agent_did="did:web:test",
        ))

        async with client:
            assert client.did  # should be connected
