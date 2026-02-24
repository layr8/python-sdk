"""Exponential backoff with a maximum delay."""

from __future__ import annotations


class Backoff:
    """Exponential backoff with a maximum delay."""

    __slots__ = ("_initial", "_max", "_current")

    def __init__(self, initial: float, maximum: float) -> None:
        self._initial = initial
        self._max = maximum
        self._current = initial

    def next(self) -> float:
        d = min(self._current, self._max)
        self._current = min(self._current * 2, self._max)
        return d

    def reset(self) -> None:
        self._current = self._initial
