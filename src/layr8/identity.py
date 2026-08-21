"""Attaching an **identity credential** — a credential about *who the sender
is*, not about what it may do.

An identity credential rides in the same ``attachments`` list, with the same
``media_type``, as a Verifiable Grant. The cloud-node tells the two apart on one
test — ``credentialSubject.scope``: present and non-empty is a grant and feeds
the policy's ``credentials`` input; absent or empty is an identity credential
and feeds ``sender_credentials``, where a grant's
``constraints.senderCredentials`` requirement can see it.

Why this is a separate path, not a wallet feature
-------------------------------------------------

The wallet SELECTS grants: it ranks candidates by how well their ``scope``
covers the outbound message. An identity credential has no scope, so it cannot
be ranked — and there is nothing here for the wallet to select BY either. The
requirement it would have to satisfy lives in the grant held by the RECIPIENT
and never reaches the sender before the call.

So an SDK that chose identity credentials automatically would have exactly one
implementable behaviour: attach everything the holder has. That is a disclosure
decision wearing the costume of a convenience feature. Which claims about a
person or an organisation a counterparty gets to see is the holder's call, made
per message.

**The caller names the credential. This module only builds the envelope.**

.. code-block:: python

    creds = await client.list_credentials()
    await client.send(Message(
        to=[peer],
        type="https://layr8.io/protocols/mcp/1.0/tools-call",
        body={"params": {"name": "place_order"}},
        attachments=[identity_attachment(creds[0].credential_jwt)],
    ))

Attaching one does NOT cost the message its grants: ``Client._with_grants``
appends the wallet's selection after attachments that are all identity
credentials.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from .message import Attachment, AttachmentData

#: The only media type the node's credential extractor keeps, matched by exact
#: string equality. Everything else — including ``application/vp+jwt``, the
#: Verifiable Presentation envelope — is dropped in silence, and the denial that
#: follows is byte-for-byte the one for attaching nothing at all.
CREDENTIAL_MEDIA_TYPE = "application/vc+jwt"


def _decode_jwt_payload(jwt: str) -> dict[str, Any]:
    """Deliberately a local copy of ``wallet._decode_jwt_payload``.

    The identity path runs BESIDE the grant wallet, not through it. Importing
    from ``wallet`` would quietly make this path depend on a module whose job is
    grant selection; ten lines of base64url is the cheaper coupling to avoid.
    """
    parts = jwt.split(".")
    if len(parts) < 2:
        return {}
    seg = parts[1]
    seg += "=" * (-len(seg) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _scope_of(jws: str) -> list[Any]:
    """``credentialSubject.scope`` of a compact JWS, as the node reads it.

    Claims are at the TOP LEVEL of the payload on this node; the ``vc`` wrapper
    is the standard alternative and both are accepted — same as
    ``parse_credential``.
    """
    payload = _decode_jwt_payload(jws)
    vc = payload["vc"] if isinstance(payload.get("vc"), dict) else payload
    cs = vc.get("credentialSubject")
    cs = cs if isinstance(cs, dict) else {}
    scope = cs.get("scope")
    return scope if isinstance(scope, list) else []


def identity_attachment(credential_jws: str) -> Attachment:
    """Build the attachment that carries one identity credential.

    *credential_jws* is the credential itself — the compact JWS, which
    ``list_credentials()`` returns as ``credential_jwt``. It is not a credential
    id: an id would mean a read from the node, and this runs inside the
    per-send write lock where an unbounded read stalls every later send (the
    reason ``Config.grant_read_timeout_ms`` exists). A JWS also lets a caller
    attach a credential the node's store has never seen.

    Raises ``ValueError``, rather than returning something the far end will
    misread, when:

    * the argument is not a compact JWS (three segments, non-empty signature).
      The node can verify nothing else, so putting it on the wire only buys a
      denial that names the wrong problem.
    * the credential carries a non-empty ``credentialSubject.scope``. That is a
      GRANT. The node would route it to the policy's ``credentials`` input, it
      would not satisfy a ``senderCredentials`` requirement, and the resulting
      denial is indistinguishable from having attached nothing. The check is
      local, exact and free — refusing here is the difference between a
      traceback at the call site and a silent misroute diagnosed at the far
      end. Grants belong to the wallet, which selects and caps them; this path
      does neither.
    """
    parts = credential_jws.split(".") if isinstance(credential_jws, str) else []
    if len(parts) != 3 or not all(parts):
        raise ValueError(
            "identity_attachment: expected a compact JWS "
            "(three non-empty dot-separated segments)"
        )

    if _scope_of(credential_jws):
        raise ValueError(
            "identity_attachment: this credential has a non-empty "
            "`credentialSubject.scope`, so it is a Verifiable Grant, not an identity "
            "credential. The node would route it to the policy's `credentials` input "
            "and it would never satisfy a `senderCredentials` requirement. Let the "
            "wallet attach grants, or pass it in `attachments` yourself."
        )

    payload = _decode_jwt_payload(credential_jws)
    vc = payload["vc"] if isinstance(payload.get("vc"), dict) else payload
    cred_id = vc.get("id") or payload.get("jti")

    return Attachment(
        # The SIGNATURE segment as the fallback, not the head of the JWT: every
        # credential from one issuer shares a header, so a head-derived id gives
        # them all the SAME attachment id — and a frame carrying two attachments
        # with one id is a frame whose second attachment may not survive.
        id=cred_id if isinstance(cred_id, str) and cred_id else f"urn:jws:{parts[2][:32]}",
        media_type=CREDENTIAL_MEDIA_TYPE,
        data=AttachmentData(jws=credential_jws),
    )


def is_identity_attachment(att: Attachment | None) -> bool:
    """Is this attachment an identity credential?

    The same test the node routes on, applied to what is actually on the
    message. Used by ``Client._with_grants`` to decide whether caller-supplied
    attachments should still displace the wallet. Nothing here trusts how the
    attachment was built: a hand-assembled one counts exactly the same.
    """
    if att is None or att.media_type != CREDENTIAL_MEDIA_TYPE:
        return False
    jws = att.data.jws if att.data is not None else None
    if not isinstance(jws, str) or len(jws.split(".")) != 3:
        return False
    return not _scope_of(jws)
