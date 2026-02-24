"""Tests for layr8.handler."""

from __future__ import annotations

import pytest

from layr8.handler import HandlerRegistry
from layr8.message import Message


async def noop_handler(msg: Message) -> None:
    return None


class TestHandlerRegistry:
    def test_register_and_lookup(self) -> None:
        registry = HandlerRegistry()
        registry.register("https://layr8.io/protocols/echo/1.0/request", noop_handler)

        entry = registry.lookup("https://layr8.io/protocols/echo/1.0/request")
        assert entry is not None
        assert entry.fn is noop_handler
        assert entry.manual_ack is False

    def test_register_with_manual_ack(self) -> None:
        registry = HandlerRegistry()
        registry.register(
            "https://layr8.io/protocols/echo/1.0/request",
            noop_handler,
            manual_ack=True,
        )
        entry = registry.lookup("https://layr8.io/protocols/echo/1.0/request")
        assert entry is not None
        assert entry.manual_ack is True

    def test_returns_none_for_unregistered(self) -> None:
        registry = HandlerRegistry()
        assert registry.lookup("unknown") is None

    def test_raises_on_duplicate(self) -> None:
        registry = HandlerRegistry()
        registry.register("https://layr8.io/protocols/echo/1.0/request", noop_handler)
        with pytest.raises(ValueError, match="already registered"):
            registry.register("https://layr8.io/protocols/echo/1.0/request", noop_handler)

    def test_derives_unique_protocols(self) -> None:
        registry = HandlerRegistry()
        registry.register("https://layr8.io/protocols/echo/1.0/request", noop_handler)
        registry.register("https://layr8.io/protocols/echo/1.0/response", noop_handler)
        registry.register("https://didcomm.org/basicmessage/2.0/message", noop_handler)

        protocols = sorted(registry.protocols())
        assert len(protocols) == 2
        assert "https://didcomm.org/basicmessage/2.0" in protocols
        assert "https://layr8.io/protocols/echo/1.0" in protocols
