"""Compat scenario types — shared by all scenarios."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class ScenarioContext:
    """Context provided to both sender and receiver scenario functions."""

    node_url: str
    api_key: str
    test_id: str
    timeout: float
    agent_did: str = ""


@dataclass
class SenderContext(ScenarioContext):
    """Extended context for sender scenarios — includes receiver DID."""

    receiver_did: str = ""


@dataclass
class ScenarioResult:
    """Result returned by sender scenario functions."""

    status: str  # "pass" | "fail"
    scenario: str
    duration_ms: int
    error: str | None = None


def elapsed_ms(start: float) -> int:
    """Return milliseconds elapsed since a time.monotonic() start value."""
    return int((time.monotonic() - start) * 1000)