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
    return env


def parse_didcomm(data: dict[str, Any]) -> Message:
    """Parse an inbound cloud-node message (context + plaintext) into a Message."""
    pt = data.get("plaintext", {})

    msg = Message(
        id=pt.get("id", ""),
        type=pt.get("type", ""),
        from_=pt.get("from", ""),
        to=pt.get("to", []),
        thread_id=pt.get("thid", ""),
        parent_thread_id=pt.get("pthid", ""),
        body=pt.get("body"),
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
