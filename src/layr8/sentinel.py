"""PASS sentinel for handler dispatch."""

from __future__ import annotations


class _Pass:
    """Sentinel returned by handlers to signal 'I don't handle this'."""

    _instance: _Pass | None = None

    def __new__(cls) -> _Pass:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "PASS"

    def __bool__(self) -> bool:
        return False


PASS = _Pass()