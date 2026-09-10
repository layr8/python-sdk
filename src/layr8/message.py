"""DIDComm v2 message types and serialization."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Credential:
    """A sender credential from the cloud-node."""

    id: str = ""
    name: str = ""


@dataclass
class MessageContext:
    """Metadata from the cloud-node, present on inbound messages."""

    recipient: str = ""
    authorized: bool = False
    sender_credentials: list[Credential] = field(default_factory=list)


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
    """
    A DIDComm v2 attachment.

    ``lastmod_time`` is ``int | str``, and this SDK does not interpret it.

    DIDComm v2 states no type for the field. Its Attachments section says
    only "OPTIONAL. A hint about when the content in this attachment was
    last modified", while the same document pins ``created_time`` and
    ``expires_time`` to "UTC Epoch Seconds (seconds since
    1970-01-01T00:00:00Z) as an integer". The authors knew how to spell
    "epoch integer" and did not spell it here, so a receiver is not
    entitled to demand one. Epoch seconds are what this SDK writes and
    what the ecosystem mostly sends; an RFC 3339 string has also been seen
    on the wire. Both arrive here unchanged.

    This annotation used to say ``int``, which let a type checker approve
    ``att.lastmod_time + 60`` on a value that was in fact a string. Read
    it by narrowing::

        t = att.lastmod_time
        if isinstance(t, int):
            when = datetime.fromtimestamp(t, tz=timezone.utc)
        elif isinstance(t, str):
            when = datetime.fromisoformat(t.replace("Z", "+00:00"))
        else:
            when = None   # the sender sent no hint

    Absent, integer and string are three different values and stay three
    different values. Nothing here folds an unhelpful hint into ``None``:
    "the sender sent no hint" and "the sender sent one I have to narrow"
    are not the same fact.
    """

    id: str = ""
    description: str = ""
    filename: str = ""
    media_type: str = ""
    format: str = ""
    lastmod_time: int | str | None = None
    byte_count: int | None = None
    data: AttachmentData = field(default_factory=AttachmentData)


@dataclass
class Message:
    """
    A DIDComm v2 message.

    Note: The ``from`` field is named ``from_`` because ``from`` is a Python
    reserved word. On the wire, it serializes as ``"from"``.

    ``attachments`` has three states, and they are three different values:

    ==========================  =====================  ==================
    the message carried         ``attachments``        ``attachments_unread``
    ==========================  =====================  ==================
    no ``attachments`` header   ``[]``                 ``None``
    a header this SDK read      the attachments        ``None``
    a header it could not read  ``None``               why
    ==========================  =====================  ==================

    Returning ``[]`` for a header nobody could read would report "this
    message carried no attachments", which is a measurement that was never
    taken. The message is still delivered either way: an authorization
    denial must not vanish because a hint travelling beside it was
    malformed.
    """

    id: str = ""
    type: str = ""
    from_: str = ""
    to: list[str] = field(default_factory=list)
    thread_id: str = ""
    parent_thread_id: str = ""
    body: Any = None
    attachments: list[Attachment] | None = field(default_factory=list)
    attachments_unread: str | None = None
    context: MessageContext | None = None

    # Internal fields (not part of the public API)
    _body_raw: Any = field(default=None, repr=False)

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


class _UnreadAttachments(Exception):
    """The ``attachments`` header could not be decoded. Internal to this module."""


def _parse_attachment(raw: dict[str, Any]) -> Attachment:
    """Parse a wire-format dict into an Attachment."""
    data_raw = raw.get("data", {})
    if not isinstance(data_raw, dict):
        raise _UnreadAttachments(
            f"attachment data is {type(data_raw).__name__}, expected an object"
        )
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


def _parse_attachments(raw: Any) -> tuple[list[Attachment] | None, str | None]:
    """
    Decode the ``attachments`` header, and never fail the message for it.

    Returns ``(attachments, unread_reason)``. An absent header is a header
    that was read and carried nothing, so it returns ``([], None)``; a
    header that could not be decoded returns ``(None, reason)``. The two
    must not share a value — see ``Message``.

    The whole header is read or not read together. Handing back the
    attachments that happened to decode, with no word about the one that
    did not, would silently drop a credential.
    """
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, f"attachments header is {type(raw).__name__}, expected a list"

    out: list[Attachment] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            return None, f"attachment {i} is {type(item).__name__}, expected an object"
        try:
            out.append(_parse_attachment(item))
        except _UnreadAttachments as exc:
            return None, f"attachment {i}: {exc}"
        except Exception as exc:  # noqa: BLE001 - a hint never costs the message
            return None, f"attachment {i}: {type(exc).__name__}: {exc}"
    return out, None


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

    attachments, attachments_unread = _parse_attachments(pt.get("attachments"))

    msg = Message(
        id=pt.get("id", ""),
        type=pt.get("type", ""),
        from_=pt.get("from", ""),
        to=pt.get("to", []),
        thread_id=pt.get("thid", ""),
        parent_thread_id=pt.get("pthid", ""),
        body=pt.get("body"),
        attachments=attachments,
        attachments_unread=attachments_unread,
        _body_raw=pt.get("body"),
    )

    ctx = data.get("context")
    if ctx:
        creds = [
            Credential(
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