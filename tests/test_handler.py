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


class TestCatchAll:
    def test_register_catch_all(self) -> None:
        registry = HandlerRegistry()
        registry.register_catch_all(noop_handler)
        entry = registry.lookup("https://any.org/protocol/1.0/anything")
        assert entry is not None
        assert entry.fn is noop_handler

    def test_specific_handler_takes_priority(self) -> None:
        registry = HandlerRegistry()

        async def specific(msg: Message) -> None:
            return None

        registry.register("https://layr8.io/protocols/echo/1.0/request", specific)
        registry.register_catch_all(noop_handler)

        entry = registry.lookup("https://layr8.io/protocols/echo/1.0/request")
        assert entry is not None
        assert entry.fn is specific

    def test_catch_all_used_when_no_specific(self) -> None:
        registry = HandlerRegistry()
        registry.register("https://layr8.io/protocols/echo/1.0/request", noop_handler)
        registry.register_catch_all(noop_handler)

        entry = registry.lookup("https://unknown.org/protocol/1.0/unknown")
        assert entry is not None
        assert entry.fn is noop_handler

    def test_returns_none_without_catch_all(self) -> None:
        registry = HandlerRegistry()
        assert registry.lookup("https://unknown.org/anything") is None

    def test_duplicate_catch_all_raises(self) -> None:
        registry = HandlerRegistry()
        registry.register_catch_all(noop_handler)
        with pytest.raises(ValueError, match="catch-all handler already registered"):
            registry.register_catch_all(noop_handler)

    def test_protocols_includes_wildcard(self) -> None:
        registry = HandlerRegistry()
        registry.register("https://layr8.io/protocols/echo/1.0/request", noop_handler)
        registry.register_catch_all(noop_handler)
        protocols = registry.protocols()
        assert "*" in protocols
        assert "https://layr8.io/protocols/echo/1.0" in protocols

    def test_protocols_no_wildcard_without_catch_all(self) -> None:
        registry = HandlerRegistry()
        registry.register("https://layr8.io/protocols/echo/1.0/request", noop_handler)
        assert "*" not in registry.protocols()