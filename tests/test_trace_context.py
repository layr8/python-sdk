"""Contract: the DIDComm ``trace_context`` plaintext header.

Parse keeps the value, marshal writes it, a malformed value never fails
parsing, and a handler's reply and handler-error problem report copy the
request's value unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from layr8 import Client, Config, Message, read_trace_context
from layr8.message import marshal_didcomm, parse_didcomm

from .test_client import MockPhoenixServer, _discard_errors, mock_server, ws_url  # noqa: F401

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"
TC = {"traceparent": TRACEPARENT, "tracestate": "vendor=value"}
REQ = "https://layr8.io/protocols/echo/1.0/request"
RESP = "https://layr8.io/protocols/echo/1.0/response"
PROBLEM = "https://didcomm.org/report-problem/2.0/problem-report"

_ABSENT = object()


def envelope(trace_context: Any = _ABSENT) -> dict[str, Any]:
    pt: dict[str, Any] = {
        "id": "req-1",
        "type": REQ,
        "from": "did:web:bob",
        "to": ["did:web:alice"],
        "thid": "thread-abc",
        "body": {},
    }
    if trace_context is not _ABSENT:
        pt["trace_context"] = trace_context
    return {"plaintext": pt}


class TestParseAndMarshal:
    def test_parse_keeps_and_marshal_writes(self) -> None:
        msg = parse_didcomm(envelope(TC))
        assert msg.trace_context == TC
        wire = marshal_didcomm(msg)
        assert wire["trace_context"] == TC
        assert wire["thid"] == "thread-abc"

    def test_absent_stays_absent(self) -> None:
        msg = parse_didcomm(envelope())
        assert msg.trace_context is None
        assert "trace_context" not in marshal_didcomm(msg)

    @pytest.mark.parametrize(
        "value",
        ["00-abc", None, [TRACEPARENT], 7, {"tracestate": "a=b"}, {"traceparent": 1}],
        ids=["string", "null", "list", "number", "no-traceparent", "non-string-traceparent"],
    )
    def test_malformed_does_not_fail_parsing(self, value: Any) -> None:
        msg = parse_didcomm(envelope(value))
        assert msg.id == "req-1"
        assert msg.trace_context is None
        assert "trace_context" not in marshal_didcomm(msg)

    def test_unknown_members_are_not_forwarded(self) -> None:
        msg = parse_didcomm(envelope({"traceparent": TRACEPARENT, "tracestate": 5, "extra": "x"}))
        assert marshal_didcomm(msg)["trace_context"] == {"traceparent": TRACEPARENT}

    def test_marshal_filters_a_caller_value(self) -> None:
        msg = Message(id="m", type=REQ, trace_context={"traceparent": TRACEPARENT, "x": "y"})
        assert marshal_didcomm(msg)["trace_context"] == {"traceparent": TRACEPARENT}

    def test_read_trace_context_is_exported(self) -> None:
        assert read_trace_context({"traceparent": TRACEPARENT}) == {"traceparent": TRACEPARENT}
        assert read_trace_context("x") is None


async def run_handler(
    server: MockPhoenixServer, handler: Any, trace_context: Any, reply_type: str
) -> list[dict[str, Any]]:
    client = Client(
        Config(node_url=ws_url(server), api_key="test-key", agent_did="did:web:alice"),
        _discard_errors,
    )
    client.handle(REQ)(handler)
    await client.connect()
    await server.send_to_client(None, None, "plugin:lobby", "message", envelope(trace_context))
    await asyncio.sleep(0.5)
    out = [
        r["payload"]
        for r in server.get_received()
        if r["event"] == "message"
        and isinstance(r["payload"], dict)
        and r["payload"].get("type") == reply_type
    ]
    await client.close()
    return out


class TestReplies:
    async def test_handler_sees_it(self, mock_server: MockPhoenixServer) -> None:  # noqa: F811
        seen: list[Any] = []

        async def handler(msg: Message) -> None:
            seen.append(msg.trace_context)
            return None

        await run_handler(mock_server, handler, TC, "none")
        assert seen == [TC]

    async def test_auto_filled_reply_copies_it(self, mock_server: MockPhoenixServer) -> None:  # noqa: F811
        async def handler(msg: Message) -> Message:
            return Message(type=RESP, body={"echo": "pong"})

        out = await run_handler(mock_server, handler, TC, RESP)
        assert len(out) == 1
        assert out[0]["trace_context"] == TC
        assert out[0]["thid"] == "thread-abc"

    async def test_reply_that_sets_its_own_keeps_it(self, mock_server: MockPhoenixServer) -> None:  # noqa: F811
        own = {"traceparent": "00-11111111111111111111111111111111-2222222222222222-00"}

        async def handler(msg: Message) -> Message:
            return Message(type=RESP, body={}, trace_context=own)

        out = await run_handler(mock_server, handler, TC, RESP)
        assert out[0]["trace_context"] == own

    async def test_handler_error_problem_report_copies_it(
        self, mock_server: MockPhoenixServer  # noqa: F811
    ) -> None:
        async def handler(msg: Message) -> Message:
            raise RuntimeError("boom")

        out = await run_handler(mock_server, handler, TC, PROBLEM)
        assert len(out) == 1
        assert out[0]["trace_context"] == TC

    @pytest.mark.parametrize("value", [_ABSENT, "00-not-an-object"], ids=["absent", "malformed"])
    async def test_no_readable_value_means_none_on_reply(
        self, mock_server: MockPhoenixServer, value: Any  # noqa: F811
    ) -> None:
        async def handler(msg: Message) -> Message:
            return Message(type=RESP, body={})

        out = await run_handler(mock_server, handler, value, RESP)
        assert len(out) == 1
        assert "trace_context" not in out[0]
