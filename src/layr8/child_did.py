"""Naming a DID that borrows a parent's authority.

A join may name the parent whose authority its DID borrows
(:attr:`Config.parent_did`). The node requires such a DID to be named
**beneath** that parent — the parent, then exactly one further segment::

    parent  did:web:acme.example:users:alice
    child   did:web:acme.example:users:alice:k7m2q9x4h3bd

and refuses a join whose DID is not, with the problem code
``e.join.plugin.child.not-beneath-parent``.

Why the shape is fixed rather than free
---------------------------------------

A cloud-node API key can restrict which DIDs it may bind. An entry is either an
exact DID or a literal prefix with a trailing ``*``, so a key can admit a whole
FAMILY of DIDs only when that family is a namespace. While a borrower's name was
unrelated to its parent, no entry shorter than the borrower's whole DID covered
it — and since the name is generated per connection, that entry cannot be
written in advance. The only key that admitted a borrower was one with **no
restrictions at all**, which admits every DID on the node.

Named beneath its parent, the family is ``<parent>:*``, and a key carrying the
parent plus that one namespace admits the parent and its borrowers and nothing
else.

The node is the control, not this module. A client that builds its own name
reaches the same socket, so the rule is enforced at the join; deriving a
conforming name here is what stops a caller having to know the rule.

The segment: random, and why not the alternatives
-------------------------------------------------

:func:`random_child_segment` returns 12 characters of Crockford base32 — 60 bits
from a cryptographic source, in an alphabet that omits ``i``, ``l``, ``o`` and
``u`` so the value survives being read off a screen and typed back.

It appears in the node's audit rows, so a person reads it. Two alternatives were
considered and both fail on something a reader would care about:

- **A counter.** There is no shared state that owns one. Two processes borrowing
  from the same parent would allocate the same number, and a collision here is
  one connection joining onto another's identity.
- **A name the operator supplies.** That is the thing this removes: a caller
  that has to hand-build a conforming DID is a caller that can get it wrong, and
  the resulting refusal happens at connect time in production.

Twelve characters is far more than collision needs (a single parent would need
on the order of a billion simultaneous borrowers before a repeat became likely)
and short enough to sit in a log line. There is no readable prefix on it: under
this rule EVERY segment beneath a parent is a borrower, so a marker saying so
would be true of every value it could ever have.

The value is generated once, when the configuration is resolved — not per join.
A reconnect therefore returns under the same DID, which is what lets the node
re-mint the same delegated credentials for it.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Literal

from .errors import Layr8Error

#: Crockford base32: the digits and lower-case letters, less ``i``, ``l``,
#: ``o`` and ``u``.
CHILD_SEGMENT_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"

#: Characters in a generated segment. 12 × 5 bits = 60 bits.
CHILD_SEGMENT_LENGTH = 12

#: Who chose the segment of a borrower's DID.
#:
#: ``""`` is a THIRD value — "this client does not report it" — and is never
#: folded into ``"client"``. A generated name and a hand-built one that conforms
#: are identical bytes on the socket, so without this the node's log could not
#: say whether a malformed borrower DID came from this library or from a
#: caller's typo.
ChildNameSource = Literal["sdk", "client"]


@dataclass(frozen=True)
class BorrowerDid:
    """A borrower's DID, and who chose its segment."""

    did: str
    #: ``""`` when no parent was named — there is no borrower, so there is
    #: nobody who chose a borrower's name. Never folded into ``"client"``.
    child_name_source: ChildNameSource | Literal[""] = ""


def random_child_segment() -> str:
    """A fresh segment for a borrower's DID.

    Each character consumes exactly five bits of one random byte, so every
    character is uniformly distributed — a modulo over a 31- or 36-character
    alphabet would not be.
    """
    raw = secrets.token_bytes(CHILD_SEGMENT_LENGTH)
    return "".join(CHILD_SEGMENT_ALPHABET[b & 0x1F] for b in raw)


def did_namespace_of(parent_did: str) -> str:
    """The API-key entry covering every DID that may borrow *parent_did*'s authority.

    Exported because a key is written by hand from it, and a key written with a
    different pattern is one the node's rule and the key disagree about.
    """
    return f"{parent_did}:*"


def is_beneath_parent(child_did: str, parent_did: str) -> bool:
    """Is *child_did* named beneath *parent_did*?

    The parent, then exactly one further non-empty segment. ``False`` for the
    parent itself, for a sibling that merely starts with the parent's text
    (``…:users:alicent``), and for a name two segments deeper.
    """
    if not child_did or not parent_did:
        return False
    prefix = f"{parent_did}:"
    if not child_did.startswith(prefix):
        return False
    segment = child_did[len(prefix):]
    return bool(segment) and ":" not in segment


def resolve_borrower_did(did: str, parent_did: str) -> BorrowerDid:
    """Settle the DID a join will use.

    Three inputs, three outcomes, and the three are kept apart on the wire:

    - **No parent named.** *did* is returned unchanged and nothing is claimed
      about who named it. This rule is about a relationship between two names
      and there is only one name here.
    - **A parent, and no DID.** The caller passes nothing but the parent; a
      segment is generated and the result is reported as ``"sdk"``.
    - **A parent and a DID.** The caller named the borrower itself, and the
      result is reported as ``"client"``. A name that is not beneath the parent
      **raises here**, rather than travelling to the node and coming back as a
      join refusal at connect time — the node still refuses it, for every client
      that is not this one.
    """
    if not parent_did:
        return BorrowerDid(did=did)

    if not did:
        return BorrowerDid(
            did=f"{parent_did}:{random_child_segment()}", child_name_source="sdk"
        )

    if not is_beneath_parent(did, parent_did):
        raise Layr8Error(
            f"agent_did {did} is not named beneath its parent {parent_did}. "
            f'A DID that borrows a parent\'s authority must be "{parent_did}:<segment>" '
            f"(exactly one further segment), and the node refuses a join that is not. "
            f"Pass parent_did and leave agent_did empty to have one generated."
        )

    return BorrowerDid(did=did, child_name_source="client")
