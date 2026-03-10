"""W3C Verifiable Presentation types for the Layr8 SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class VerifiedPresentation:
    """Result of verifying a signed presentation."""

    presentation: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, Any] = field(default_factory=dict)
