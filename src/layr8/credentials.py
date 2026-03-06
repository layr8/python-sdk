"""W3C Verifiable Credential types for the Layr8 SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Credential:
    """A W3C Verifiable Credential for signing.

    The ``context`` field maps to ``@context`` in the JSON payload.
    """

    credential_subject: dict[str, Any] = field(default_factory=dict)
    context: list[str] | None = None
    id: str = ""
    type: list[str] | None = None
    issuer: str = ""
    valid_from: str = ""
    valid_until: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict suitable for the REST API (uses ``@context``)."""
        d: dict[str, Any] = {"credentialSubject": self.credential_subject}
        if self.context is not None:
            d["@context"] = self.context
        if self.id:
            d["id"] = self.id
        if self.type is not None:
            d["type"] = self.type
        if self.issuer:
            d["issuer"] = self.issuer
        if self.valid_from:
            d["validFrom"] = self.valid_from
        if self.valid_until:
            d["validUntil"] = self.valid_until
        return d


@dataclass
class VerifiedCredential:
    """Result of verifying a signed credential."""

    credential: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, Any] = field(default_factory=dict)


@dataclass
class StoredCredential:
    """A credential stored in the node's credential store."""

    id: str = ""
    holder_did: str = ""
    credential_jwt: str = ""
    issuer_did: str = ""
    valid_until: str = ""
