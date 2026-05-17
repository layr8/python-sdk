"""Tests for layr8.sentinel."""

from __future__ import annotations


class TestPassSentinel:
    def test_is_singleton(self) -> None:
        from layr8.sentinel import _Pass
        a = _Pass()
        b = _Pass()
        assert a is b

    def test_repr(self) -> None:
        from layr8.sentinel import PASS
        assert repr(PASS) == "PASS"

    def test_is_falsy(self) -> None:
        from layr8.sentinel import PASS
        assert not PASS
        assert bool(PASS) is False

    def test_is_not_none(self) -> None:
        from layr8.sentinel import PASS
        assert PASS is not None

    def test_importable_from_package(self) -> None:
        from layr8 import PASS
        assert repr(PASS) == "PASS"