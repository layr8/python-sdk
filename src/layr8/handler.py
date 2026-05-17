"""Handler registry for DIDComm message types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .message import Message
from .sentinel import _Pass

HandlerFn = Callable[[Message], Awaitable[Message | None | _Pass]]


@dataclass
class HandlerEntry:
    fn: HandlerFn


class HandlerRegistry:
    """Handler registry mapping message types to handlers.

    All registration must complete before connect().
    """

    def __init__(self) -> None:
        self._handlers: dict[str, HandlerEntry] = {}
        self._catch_all: HandlerEntry | None = None

    def register(
        self,
        msg_type: str,
        fn: HandlerFn,
    ) -> None:
        if msg_type in self._handlers:
            raise ValueError(
                f'handler already registered for message type "{msg_type}"'
            )
        self._handlers[msg_type] = HandlerEntry(fn=fn)

    def register_catch_all(self, fn: HandlerFn) -> None:
        if self._catch_all is not None:
            raise ValueError("catch-all handler already registered")
        self._catch_all = HandlerEntry(fn=fn)

    def lookup(self, msg_type: str) -> HandlerEntry | None:
        entry = self._handlers.get(msg_type)
        if entry is not None:
            return entry
        return self._catch_all

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
        result = list(seen)
        if self._catch_all is not None:
            result.append("*")
        return result


def _derive_protocol(msg_type: str) -> str:
    """Extract the protocol base URI by removing the last path segment."""
    idx = msg_type.rfind("/")
    return msg_type if idx == -1 else msg_type[:idx]