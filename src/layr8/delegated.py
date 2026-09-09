"""What a join reply says about the parent's wallet."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: How completely the node read the parent's wallet.
#:
#: - ``"complete"`` — it was read and every grant in it was delegated.
#:   ``credentials`` is the whole answer, and ``[]`` here is the measured
#:   statement that the parent holds no grants.
#: - ``"partial"`` — it was read and at least one grant could **not** be
#:   delegated. ``credentials`` holds the rest, and there is authority the
#:   parent has that this connection will never get. The node's log says why.
#: - ``"unread"`` — it could not be read at all. ``credentials`` is ``[]`` and
#:   that ``[]`` measures nothing.
DelegationStatus = Literal["complete", "partial", "unread"]

_DELEGATION_STATUSES = ("complete", "partial", "unread")


@dataclass(frozen=True)
class DelegatedCredential:
    """One credential the node signed for this DID out of what its parent holds.

    *credential_jwt* is a compact JWS, ready to attach to an outbound message as
    ``application/vc+jwt`` — the same shape ``GET /api/v1/credentials`` returns,
    so the wallet parses it with no special case.

    **It exists nowhere but the join reply.** The node stores nothing about it:
    a credential belonging to a connection has the lifetime of that connection.
    There is no endpoint that will hand it back, and losing the join reply means
    rejoining to be issued a new one.
    """

    #: The credential's own ``id``.
    id: str = ""
    #: The parent credential it cites in
    #: ``credentialSubject.delegation.parentCapability``.
    parent_capability: str = ""
    #: The signed credential, as a compact JWS.
    credential_jwt: str = ""


@dataclass(frozen=True)
class DelegatedCredentialsReading:
    """What one join learned about the parent's wallet.

    The node sends an object rather than a bare array precisely so that
    ``"unread"`` has a value of its own. When it was an array, an unreadable
    wallet arrived as ``[]`` — the same value that means "read, and it grants
    nothing" — and that is the reassuring one of the two: a client acting on it
    sends its messages bare and gets back a denial naming a grant.
    """

    status: DelegationStatus
    credentials: list[DelegatedCredential] = field(default_factory=list)


def parse_delegated_credentials(raw: Any) -> DelegatedCredentialsReading | None:
    """A join reply's ``delegated_credentials``, or ``None`` if it is not a reading.

    Anything that is not a well-formed reading — absent, a list (an older node,
    before ``status`` existed), a status this build does not know — is ``None``,
    which means "no reading". It is never coerced into
    ``DelegatedCredentialsReading("complete", [])``: that would state that a
    wallet was read and grants nothing, which is the one thing none of those
    inputs says.
    """
    if not isinstance(raw, dict):
        return None
    status = raw.get("status")
    if status not in _DELEGATION_STATUSES:
        return None
    credentials = raw.get("credentials")
    if not isinstance(credentials, list):
        return None

    parsed = [
        DelegatedCredential(
            id=str(entry.get("id", "")),
            parent_capability=str(entry.get("parent_capability", "")),
            credential_jwt=str(entry.get("credential_jwt", "")),
        )
        for entry in credentials
        if isinstance(entry, dict)
    ]
    return DelegatedCredentialsReading(status=status, credentials=parsed)
