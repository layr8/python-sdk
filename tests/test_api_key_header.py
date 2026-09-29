"""The API key reaches the node in the x-api-key header and nowhere else.

Not in the socket URL, not in an error or its traceback, not in the frame
locals an error reporter captures, not in the repr of a Config. Every test
uses a made-up key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from http import HTTPStatus
from typing import Any

import pytest
import websockets.asyncio.server

from layr8 import Client, Config, SDKError
from layr8.channel import PhoenixChannel
from layr8.config import resolve_config

FAKE_KEY = "fake_key_0123_not-a-real-key-do-not-log"


def _discard(err: SDKError) -> None:
    pass


class RecordingNode:
    """A mock node that records each WebSocket upgrade request."""

    def __init__(self, reject_with: HTTPStatus | None = None) -> None:
        self.paths: list[str] = []
        self.headers: list[dict[str, str]] = []
        self._reject_with = reject_with
        self._server: websockets.asyncio.server.Server | None = None
        self.port = 0

    def _process_request(self, connection: Any, request: Any) -> Any:
        self.paths.append(request.path)
        self.headers.append({k.lower(): v for k, v in request.headers.raw_items()})
        if self._reject_with is not None:
            return connection.respond(self._reject_with, "unauthorized\n")
        return None

    async def _handler(self, ws: websockets.asyncio.server.ServerConnection) -> None:
        try:
            async for raw in ws:
                arr = json.loads(raw)
                if arr[3] == "phx_join":
                    await ws.send(json.dumps(
                        [arr[1], arr[1], arr[2], "phx_reply", {"status": "ok", "response": {}}]
                    ))
                elif arr[1] is not None:
                    await ws.send(json.dumps(
                        [None, arr[1], arr[2], "phx_reply", {"status": "ok", "response": {}}]
                    ))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def start(self) -> None:
        self._server = await websockets.asyncio.server.serve(
            self._handler, "127.0.0.1", 0, process_request=self._process_request
        )
        self.port = list(self._server.sockets)[0].getsockname()[1]

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/plugin_socket/websocket"

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()


def _everything_an_error_exposes(exc: BaseException) -> str:
    """The formatted traceback plus the repr of this SDK's frame locals.

    Error reporters (Sentry and the like) capture frame locals; a local that
    holds the key, or a URL or dict containing it, leaks it there. Only this
    SDK's own frames are checked: the websockets library necessarily holds the
    header it is about to send in its own locals, and this SDK cannot change
    that.
    """
    parts = ["".join(traceback.format_exception(exc))]
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        tb = cur.__traceback__
        while tb is not None:
            in_sdk = "/layr8/" in tb.tb_frame.f_code.co_filename
            for name, value in tb.tb_frame.f_locals.items():
                if in_sdk and name != "self":
                    parts.append(f"{name}={value!r}")
            tb = tb.tb_next
        cur = cur.__cause__ or cur.__context__
    return "\n".join(parts)


async def test_upgrade_carries_key_in_header_and_not_in_url() -> None:
    node = RecordingNode()
    await node.start()
    try:
        client = Client(Config(node_url=node.url, api_key=FAKE_KEY, agent_did="did:web:alice", attach_grants=False), _discard)
        await client.connect()
        await client.close()
    finally:
        await node.close()

    assert node.headers, "the node saw no upgrade request"
    assert node.headers[0].get("x-api-key") == FAKE_KEY
    assert FAKE_KEY not in node.paths[0]
    assert "api_key" not in node.paths[0]
    assert "vsn=2.0.0" in node.paths[0]


@pytest.mark.parametrize(
    "node_url",
    [
        # A node_url with no scheme made websockets raise
        # InvalidURI quoting the full URL, key included.
        "node.localhost:4000/plugin_socket/websocket",
        "ftp://node.localhost/plugin_socket/websocket",
    ],
)
async def test_misconfigured_node_url_error_does_not_carry_key(node_url: str) -> None:
    client = Client(Config(node_url=node_url, api_key=FAKE_KEY, agent_did="did:web:alice", attach_grants=False), _discard)
    with pytest.raises(Exception) as info:
        await client.connect()
    exposed = _everything_an_error_exposes(info.value)
    assert FAKE_KEY not in exposed


async def test_refused_upgrade_error_does_not_carry_key() -> None:
    node = RecordingNode(reject_with=HTTPStatus.UNAUTHORIZED)
    await node.start()
    try:
        ch = PhoenixChannel(node.url, FAKE_KEY, "did:web:alice", on_message=lambda m: None)
        with pytest.raises(Exception) as info:
            await ch.connect([])
    finally:
        await node.close()
    assert node.headers[0].get("x-api-key") == FAKE_KEY
    assert FAKE_KEY not in _everything_an_error_exposes(info.value)


def test_config_reprs_do_not_carry_key(caplog: pytest.LogCaptureFixture) -> None:
    cfg = Config(node_url="ws://node.localhost/plugin_socket/websocket", api_key=FAKE_KEY, agent_did="did:web:alice")
    resolved = resolve_config(cfg)
    client = Client(cfg, _discard)
    with caplog.at_level(logging.INFO):
        logging.getLogger("test").info("config %s %r %s %r %r", cfg, cfg, resolved, resolved, client)
    for text in (repr(cfg), str(cfg), repr(resolved), str(resolved), repr(client), caplog.text):
        assert FAKE_KEY not in text
    # The key is still there for the SDK to use.
    assert resolved.api_key == FAKE_KEY
    assert "did:web:alice" in repr(cfg)
