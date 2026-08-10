"""Tests for layr8.mcp — the JSON-RPC envelope, type mapping and result unwrapping."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from layr8 import Client, Config, Message, ProblemReportError, SDKError
from layr8.mcp import DEFAULT_MCP_BASE, McpBinding, McpError, type_for_method


def _discard_errors(err: SDKError) -> None:
    pass


class StubClient:
    """A stand-in for Client that answers the one call McpPeer makes.

    It lets the envelope, the type mapping and the unwrapping be tested as one
    path — the parts a peer actually sees — without a node.
    """

    def __init__(self, reply: Any) -> None:
        self._reply = reply
        self.sent: list[tuple[Message, float]] = []

    async def request(self, msg: Message, *, timeout: float = 30.0) -> Message:
        self.sent.append((msg, timeout))
        if isinstance(self._reply, Exception):
            raise self._reply
        return Message(type=msg.type + "-result", body=self._reply)


def peer_answering(reply: Any):
    stub = StubClient(reply)
    return McpBinding(stub, DEFAULT_MCP_BASE).peer("did:web:loom.localhost"), stub  # type: ignore[arg-type]


class TestTypeForMethod:
    def test_slashes_become_hyphens(self) -> None:
        base = "https://layr8.io/protocols/mcp/1.0"
        assert type_for_method(base, "tools/call") == f"{base}/tools-call"
        assert type_for_method(base, "tools/list") == f"{base}/tools-list"
        assert type_for_method(base, "initialize") == f"{base}/initialize"

    def test_the_default_base_is_mcp_1_0(self) -> None:
        assert DEFAULT_MCP_BASE == "https://layr8.io/protocols/mcp/1.0"


class TestCall:
    async def test_sends_one_json_rpc_request_and_returns_its_result(self) -> None:
        peer, stub = peer_answering({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

        assert await peer.call("tools/call", {"name": "send"}) == {"ok": True}

        msg, _timeout = stub.sent[0]
        assert msg.type == "https://layr8.io/protocols/mcp/1.0/tools-call"
        assert msg.to == ["did:web:loom.localhost"]
        assert msg.body["jsonrpc"] == "2.0"
        assert msg.body["method"] == "tools/call"
        assert msg.body["params"] == {"name": "send"}
        assert isinstance(msg.body["id"], int)

    async def test_omits_params_entirely_when_there_are_none(self) -> None:
        peer, stub = peer_answering({"result": {}})
        await peer.call("tools/list")
        assert "params" not in stub.sent[0][0].body

    async def test_ids_are_unique_so_two_in_flight_cannot_be_confused(self) -> None:
        peer, stub = peer_answering({"result": None})
        await peer.call("tools/list")
        await peer.call("tools/list")
        assert stub.sent[0][0].body["id"] != stub.sent[1][0].body["id"]

    async def test_a_json_rpc_error_becomes_an_mcp_error(self) -> None:
        peer, _ = peer_answering(
            {"error": {"code": -32_602, "message": "unknown tool", "data": {"tool": "x"}}}
        )
        with pytest.raises(McpError) as exc:
            await peer.call("tools/call", {"name": "x"})
        assert exc.value.code == -32_602
        assert exc.value.message == "unknown tool"
        assert exc.value.data == {"tool": "x"}

    async def test_a_denial_stays_a_problem_report_not_an_mcp_error(self) -> None:
        # The usual cause is a Verifiable Grant that never reached the wire
        # rather than one that is misconfigured — see layr8.wallet.
        peer, _ = peer_answering(
            ProblemReportError(code="e.m.authz.denied", comment="no grant covers this call")
        )
        with pytest.raises(ProblemReportError):
            await peer.call("tools/call", {"name": "send"})

    async def test_a_lapsed_deadline_propagates(self) -> None:
        peer, _ = peer_answering(asyncio.TimeoutError())
        with pytest.raises(asyncio.TimeoutError):
            await peer.call("tools/list")

    async def test_a_reply_that_is_neither_result_nor_error_is_not_silently_none(self) -> None:
        peer, _ = peer_answering({"something": "else"})
        with pytest.raises(McpError):
            await peer.call("tools/list")

    async def test_a_null_result_is_a_result(self) -> None:
        peer, _ = peer_answering({"jsonrpc": "2.0", "id": 1, "result": None})
        assert await peer.call("tools/call") is None

    async def test_the_timeout_reaches_the_request(self) -> None:
        peer, stub = peer_answering({"result": {}})
        await peer.call("tools/list", timeout=5.0)
        assert stub.sent[0][1] == 5.0


class TestConvenienceCalls:
    async def test_call_tool_builds_the_params_mcp_specifies(self) -> None:
        peer, stub = peer_answering({"result": {"content": []}})
        await peer.call_tool("send_email", {"to": "bob@example.com"})

        msg, _ = stub.sent[0]
        assert msg.body["method"] == "tools/call"
        assert msg.body["params"] == {
            "name": "send_email",
            "arguments": {"to": "bob@example.com"},
        }

    async def test_call_tool_sends_an_empty_arguments_object_rather_than_omitting_it(
        self,
    ) -> None:
        peer, stub = peer_answering({"result": {}})
        await peer.call_tool("ping")
        assert stub.sent[0][0].body["params"] == {"name": "ping", "arguments": {}}

    async def test_list_tools_unwraps_the_tools_array(self) -> None:
        peer, _ = peer_answering({"result": {"tools": [{"name": "send_email"}]}})
        assert await peer.list_tools() == [{"name": "send_email"}]

    async def test_list_tools_on_a_peer_with_no_tools_key_is_empty(self) -> None:
        peer, _ = peer_answering({"result": {}})
        assert await peer.list_tools() == []

    async def test_list_tools_propagates_an_error_rather_than_reading_as_no_tools(
        self,
    ) -> None:
        # "I could not ask" and "there are none" are different answers, and
        # collapsing them is how a dead credential reads as an empty tool surface.
        peer, _ = peer_answering(asyncio.TimeoutError())
        with pytest.raises(asyncio.TimeoutError):
            await peer.list_tools()

    async def test_initialize_names_the_sdk_by_default(self) -> None:
        peer, stub = peer_answering({"result": {"protocolVersion": "2025-06-18"}})
        await peer.initialize()

        msg, _ = stub.sent[0]
        assert msg.body["method"] == "initialize"
        assert msg.body["params"]["clientInfo"]["name"] == "layr8-python-sdk"


def _client() -> Client:
    return Client(
        Config(
            node_url="ws://127.0.0.1:1/plugin_socket/websocket",
            api_key="test-api-key",
            agent_did="did:web:alice.localhost",
        ),
        _discard_errors,
    )


class TestClientMcp:
    def test_subscribes_the_client_to_the_mcp_protocol_base(self) -> None:
        # Without the subscription the node has nowhere to deliver the
        # `{base}/…-result` reply, and every call times out.
        client = _client()
        binding = client.mcp()

        assert binding.base == DEFAULT_MCP_BASE
        assert DEFAULT_MCP_BASE in client._registry.protocols()

    def test_is_idempotent_per_base(self) -> None:
        # A second call must not raise the way a duplicate handle() would.
        client = _client()
        first = client.mcp()
        second = client.mcp()

        assert first.base == second.base
        assert client._registry.protocols().count(DEFAULT_MCP_BASE) == 1

    def test_a_custom_base_subscribes_to_that_base(self) -> None:
        client = _client()
        binding = client.mcp("https://example.com/protocols/mcp/2.0")

        assert binding.base == "https://example.com/protocols/mcp/2.0"
        assert "https://example.com/protocols/mcp/2.0" in client._registry.protocols()

    def test_peer_binds_a_caller_to_a_did(self) -> None:
        peer = _client().mcp().peer("did:web:loom.localhost")
        assert peer.did == "did:web:loom.localhost"
        assert peer.base == DEFAULT_MCP_BASE

    async def test_calling_through_an_unconnected_client_is_an_error_not_a_hang(
        self,
    ) -> None:
        peer = _client().mcp().peer("did:web:loom.localhost")
        with pytest.raises(Exception, match="not connected"):
            await peer.list_tools()
