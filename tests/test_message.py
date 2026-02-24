"""Tests for layr8.message."""

from __future__ import annotations

from dataclasses import dataclass

from layr8.message import (
    Message,
    generate_id,
    marshal_didcomm,
    parse_didcomm,
)


class TestGenerateId:
    def test_returns_non_empty_string(self) -> None:
        assert generate_id()

    def test_returns_unique_values(self) -> None:
        ids = {generate_id() for _ in range(100)}
        assert len(ids) == 100


class TestMarshalDIDComm:
    def test_serializes_all_fields(self) -> None:
        msg = Message(
            id="msg-1",
            type="https://layr8.io/protocols/echo/1.0/request",
            from_="did:web:alice",
            to=["did:web:bob"],
            thread_id="thread-1",
            parent_thread_id="parent-1",
            body={"message": "hello"},
        )
        data = marshal_didcomm(msg)
        assert data["id"] == "msg-1"
        assert data["type"] == "https://layr8.io/protocols/echo/1.0/request"
        assert data["from"] == "did:web:alice"
        assert data["to"] == ["did:web:bob"]
        assert data["thid"] == "thread-1"
        assert data["pthid"] == "parent-1"
        assert data["body"]["message"] == "hello"

    def test_omits_thid_pthid_when_empty(self) -> None:
        msg = Message(id="msg-1", type="test", from_="did:web:alice")
        data = marshal_didcomm(msg)
        assert "thid" not in data
        assert "pthid" not in data


class TestParseDIDComm:
    def test_parses_envelope_with_context(self) -> None:
        data = {
            "context": {
                "recipient": "did:web:alice",
                "authorized": True,
                "sender_credentials": [
                    {"credential_subject": {"id": "did:web:bob", "name": "Bob"}}
                ],
            },
            "plaintext": {
                "id": "msg-1",
                "type": "https://didcomm.org/basicmessage/2.0/message",
                "from": "did:web:bob",
                "to": ["did:web:alice"],
                "thid": "thread-1",
                "body": {"content": "hello"},
            },
        }
        msg = parse_didcomm(data)
        assert msg.id == "msg-1"
        assert msg.from_ == "did:web:bob"
        assert msg.thread_id == "thread-1"
        assert msg.context is not None
        assert msg.context.authorized is True
        assert msg.context.sender_credentials[0].name == "Bob"

    def test_parses_without_context(self) -> None:
        data = {
            "plaintext": {
                "id": "msg-1",
                "type": "test",
                "from": "did:web:bob",
                "body": {"key": "value"},
            }
        }
        msg = parse_didcomm(data)
        assert msg.id == "msg-1"
        assert msg.context is None


class TestUnmarshalBody:
    def test_returns_raw_dict(self) -> None:
        msg = Message(body={"hello": "world"}, _body_raw={"hello": "world"})
        body = msg.unmarshal_body()
        assert body["hello"] == "world"

    def test_unmarshals_into_dataclass(self) -> None:
        @dataclass
        class EchoRequest:
            message: str

        msg = Message(_body_raw={"message": "ping"})
        body = msg.unmarshal_body(EchoRequest)
        assert isinstance(body, EchoRequest)
        assert body.message == "ping"


class TestAck:
    def test_calls_ack_fn(self) -> None:
        called_with: list[str] = []
        msg = Message(id="msg-1", _ack_fn=lambda mid: called_with.append(mid))
        msg.ack()
        assert called_with == ["msg-1"]

    def test_noop_without_ack_fn(self) -> None:
        msg = Message(id="msg-1")
        msg.ack()  # should not raise
