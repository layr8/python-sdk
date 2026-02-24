"""Handler registry for DIDComm message types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .message import Message

HandlerFn = Callable[[Message], Awaitable[Message | None]]


@dataclass
class HandlerEntry:
    fn: HandlerFn
    manual_ack: bool = False


class HandlerRegistry:
    """Thread-safe handler registry mapping message types to handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, HandlerEntry] = {}

    def register(
        self,
        msg_type: str,
        fn: HandlerFn,
        *,
        manual_ack: bool = False,
    ) -> None:
        if msg_type in self._handlers:
            raise ValueError(
                f'handler already registered for message type "{msg_type}"'
            )
        self._handlers[msg_type] = HandlerEntry(fn=fn, manual_ack=manual_ack)

    def lookup(self, msg_type: str) -> HandlerEntry | None:
        return self._handlers.get(msg_type)

    def protocols(self) -> list[str]:
        """
        Return unique protocol base URIs derived from registered handler types.

        e.g. "https://layr8.io/protocols/echo/1.0/request"
             → "https://layr8.io/protocols/echo/1.0"
        """
        seen: set[str] = set()
        for msg_type in self._handlers:
            proto = _derive_protocol(msg_type)
            seen.add(proto)
        return list(seen)


def _derive_protocol(msg_type: str) -> str:
    """Extract the protocol base URI by removing the last path segment."""
    idx = msg_type.rfind("/")
    return msg_type if idx == -1 else msg_type[:idx]
