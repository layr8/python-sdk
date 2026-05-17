"""DIDComm v2 message types and serialization."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class SenderCredential:
    """A sender credential from the cloud-node."""

    id: str = ""
    name: str = ""


@dataclass
class MessageContext:
    """Metadata from the cloud-node, present on inbound messages."""

    recipient: str = ""
    authorized: bool = False
    sender_credentials: list[SenderCredential] = field(default_factory=list)


@dataclass
class AttachmentData:
    """Attachment payload per DIDComm v2 spec."""
    base64: str = ""
    json: Any = None
    jws: Any = None
    hash: str = ""
    links: list[str] = field(default_factory=list)


@dataclass
class Attachment:
    """A DIDComm v2 attachment."""
    id: str = ""
    description: str = ""
    filename: str = ""
    media_type: str = ""
    format: str = ""
    lastmod_time: int | None = None
    byte_count: int | None = None
    data: AttachmentData = field(default_factory=AttachmentData)


@dataclass
class Message:
    """
    A DIDComm v2 message.

    Note: The ``from`` field is named ``from_`` because ``from`` is a Python
    reserved word. On the wire, it serializes as ``"from"``.
    """

    id: str = ""
    type: str = ""
    from_: str = ""
    to: list[str] = field(default_factory=list)
    thread_id: str = ""
    parent_thread_id: str = ""
    body: Any = None
    attachments: list[Attachment] = field(default_factory=list)
    context: MessageContext | None = None

    # Internal fields (not part of the public API)
    _body_raw: Any = field(default=None, repr=False)
    _ack_fn: Callable[..., Any] | None = field(default=None, repr=False)

    def unmarshal_body(self, cls: type | None = None) -> Any:
        """
        Decode the message body.

        If *cls* is a dataclass, construct an instance from the body dict.
        Otherwise returns the raw dict.
        """
        raw = self._body_raw if self._body_raw is not None else self.body
        if cls is not None and hasattr(cls, "__dataclass_fields__"):
            return cls(**raw)
        return raw

    def ack(self) -> None:
        """Manually acknowledge this message (only with manual_ack=True)."""
        if self._ack_fn is not None:
            self._ack_fn(self.id)


def generate_id() -> str:
    """Return a new unique message ID."""
    return str(uuid.uuid4())


def _marshal_attachment(att: Attachment) -> dict[str, Any]:
    """Convert an Attachment to a wire-format dict, omitting empty/None fields."""
    d: dict[str, Any] = {}
    if att.id:
        d["id"] = att.id
    if att.description:
        d["description"] = att.description
    if att.filename:
        d["filename"] = att.filename
    if att.media_type:
        d["media_type"] = att.media_type
    if att.format:
        d["format"] = att.format
    if att.lastmod_time is not None:
        d["lastmod_time"] = att.lastmod_time
    if att.byte_count is not None:
        d["byte_count"] = att.byte_count
    data: dict[str, Any] = {}
    if att.data.base64:
        data["base64"] = att.data.base64
    if att.data.json is not None:
        data["json"] = att.data.json
    if att.data.jws is not None:
        data["jws"] = att.data.jws
    if att.data.hash:
        data["hash"] = att.data.hash
    if att.data.links:
        data["links"] = att.data.links
    if data:
        d["data"] = data
    return d


def _parse_attachment(raw: dict[str, Any]) -> Attachment:
    """Parse a wire-format dict into an Attachment."""
    data_raw = raw.get("data", {})
    data = AttachmentData(
        base64=data_raw.get("base64", ""),
        json=data_raw.get("json"),
        jws=data_raw.get("jws"),
        hash=data_raw.get("hash", ""),
        links=data_raw.get("links", []),
    )
    return Attachment(
        id=raw.get("id", ""),
        description=raw.get("description", ""),
        filename=raw.get("filename", ""),
        media_type=raw.get("media_type", ""),
        format=raw.get("format", ""),
        lastmod_time=raw.get("lastmod_time"),
        byte_count=raw.get("byte_count"),
        data=data,
    )


def marshal_didcomm(msg: Message) -> dict[str, Any]:
    """Serialize a Message into DIDComm wire format (dict ready for JSON)."""
    env: dict[str, Any] = {
        "id": msg.id,
        "type": msg.type,
        "from": msg.from_,
        "to": msg.to,
        "body": msg.body if msg.body is not None else (msg._body_raw or {}),
    }
    if msg.thread_id:
        env["thid"] = msg.thread_id
    if msg.parent_thread_id:
        env["pthid"] = msg.parent_thread_id
    if msg.attachments:
        env["attachments"] = [_marshal_attachment(a) for a in msg.attachments]
    return env


def parse_didcomm(data: dict[str, Any]) -> Message:
    """Parse an inbound cloud-node message (context + plaintext) into a Message."""
    pt = data.get("plaintext", {})

    attachments = [_parse_attachment(a) for a in pt.get("attachments", [])]

    msg = Message(
        id=pt.get("id", ""),
        type=pt.get("type", ""),
        from_=pt.get("from", ""),
        to=pt.get("to", []),
        thread_id=pt.get("thid", ""),
        parent_thread_id=pt.get("pthid", ""),
        body=pt.get("body"),
        attachments=attachments,
        _body_raw=pt.get("body"),
    )

    ctx = data.get("context")
    if ctx:
        creds = [
            SenderCredential(
                id=c.get("credential_subject", {}).get("id", ""),
                name=c.get("credential_subject", {}).get("name", ""),
            )
            for c in ctx.get("sender_credentials", [])
        ]
        msg.context = MessageContext(
            recipient=ctx.get("recipient", ""),
            authorized=ctx.get("authorized", False),
            sender_credentials=creds,
        )

    return msg