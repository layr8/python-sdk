"""Tests for layr8.message."""

from __future__ import annotations

from dataclasses import dataclass

from layr8.message import (
    Attachment,
    AttachmentData,
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


class TestAttachmentMarshal:
    def test_marshal_with_attachments(self) -> None:
        """Message with attachments marshals correctly."""
        msg = Message(
            id="msg-1",
            type="test",
            from_="did:web:alice",
            to=["did:web:bob"],
            attachments=[
                Attachment(
                    id="att-1",
                    media_type="application/json",
                    data=AttachmentData(base64="eyJoZWxsbyI6IndvcmxkIn0="),
                ),
            ],
        )
        data = marshal_didcomm(msg)
        assert "attachments" in data
        assert len(data["attachments"]) == 1
        att = data["attachments"][0]
        assert att["id"] == "att-1"
        assert att["media_type"] == "application/json"
        assert att["data"]["base64"] == "eyJoZWxsbyI6IndvcmxkIn0="

    def test_marshal_without_attachments(self) -> None:
        """No attachments field when list is empty."""
        msg = Message(id="msg-1", type="test", from_="did:web:alice")
        data = marshal_didcomm(msg)
        assert "attachments" not in data

    def test_attachment_omits_empty_fields(self) -> None:
        """Empty/None fields are not in marshaled attachment output."""
        msg = Message(
            id="msg-1",
            type="test",
            from_="did:web:alice",
            attachments=[
                Attachment(
                    id="att-1",
                    data=AttachmentData(base64="abc123"),
                ),
            ],
        )
        data = marshal_didcomm(msg)
        att = data["attachments"][0]
        assert "id" in att
        assert "data" in att
        # Empty string fields should be omitted
        assert "description" not in att
        assert "filename" not in att
        assert "media_type" not in att
        assert "format" not in att
        # None fields should be omitted
        assert "lastmod_time" not in att
        assert "byte_count" not in att
        # Empty fields in data should be omitted
        assert "json" not in att["data"]
        assert "jws" not in att["data"]
        assert "hash" not in att["data"]
        assert "links" not in att["data"]


class TestAttachmentParse:
    def test_parse_with_attachments(self) -> None:
        """Inbound message with attachments parses correctly."""
        data = {
            "plaintext": {
                "id": "msg-1",
                "type": "test",
                "from": "did:web:bob",
                "to": ["did:web:alice"],
                "body": {},
                "attachments": [
                    {
                        "id": "att-1",
                        "description": "A test file",
                        "media_type": "text/plain",
                        "data": {
                            "base64": "aGVsbG8=",
                            "hash": "abc123",
                        },
                    },
                ],
            },
        }
        msg = parse_didcomm(data)
        assert len(msg.attachments) == 1
        att = msg.attachments[0]
        assert att.id == "att-1"
        assert att.description == "A test file"
        assert att.media_type == "text/plain"
        assert att.data.base64 == "aGVsbG8="
        assert att.data.hash == "abc123"

    def test_attachment_roundtrip(self) -> None:
        """Marshal then parse preserves attachments."""
        original = Message(
            id="msg-1",
            type="test",
            from_="did:web:alice",
            to=["did:web:bob"],
            body={"key": "value"},
            attachments=[
                Attachment(
                    id="att-1",
                    description="Test attachment",
                    filename="test.json",
                    media_type="application/json",
                    format="json",
                    lastmod_time=1234567890,
                    byte_count=42,
                    data=AttachmentData(
                        base64="eyJoZWxsbyI6IndvcmxkIn0=",
                        hash="sha256-abc",
                        links=["https://example.com/file"],
                    ),
                ),
            ],
        )
        wire = marshal_didcomm(original)
        parsed = parse_didcomm({"plaintext": wire})

        assert len(parsed.attachments) == 1
        att = parsed.attachments[0]
        assert att.id == "att-1"
        assert att.description == "Test attachment"
        assert att.filename == "test.json"
        assert att.media_type == "application/json"
        assert att.format == "json"
        assert att.lastmod_time == 1234567890
        assert att.byte_count == 42
        assert att.data.base64 == "eyJoZWxsbyI6IndvcmxkIn0="
        assert att.data.hash == "sha256-abc"
        assert att.data.links == ["https://example.com/file"]