"""
Attaching Verifiable Grants to outbound messages.

Why this exists
---------------

The cloud-node REQUIRES a Verifiable Grant for any message its policy does not
allow outright, and until now nothing in this SDK attached one. There was no
enforcement on outgoing requests — there was no *mechanism*. An agent that
connected directly, on any protocol, sent nothing and was denied with "no grant
covers this call": a message that reads as "your grant is misconfigured" when
the truth is "no credential was ever put on the wire".

That misreading is the expensive part. Two teams spent days on it — checking the
grant, the Space policy, whether the PDP expanded ``messageTypes: ["*"]``. The
sender is the only party that knows it attached nothing, so the sender is the
only one that can say so: see ``Config.on_grant_miss``.

Cross-language contract: ``contracts/sender-cn-vg-attachment.md``. The Node
SDK's ``src/wallet.ts`` is the same abstraction.

The attachment shape is load-bearing
------------------------------------

``media_type`` is the ONLY thing the node's credential extractor filters on: it
keeps attachments whose media type is exactly ``"application/vc+jwt"`` and drops
every other one SILENTLY, before looking at the data at all. A Verifiable
Presentation (``application/vp+jwt``) is discarded on that rule, and the denial
that follows is byte-for-byte the one you get for attaching nothing — which is
how a partner team spent a day looking at a grant that was fine.

``data.jws`` is the primary place the JWS is read from, and what this SDK
writes. ``data.base64`` is NOT dropped: the extractor falls back to it and
base64url-decodes it. ``data.jws`` is still the right choice — it is the field
the extractor reaches for first and the one the whole ecosystem writes — but the
reason is "primary path", not "the alternative is discarded".

Over-attaching is free; under-attaching is not
----------------------------------------------

``grant.rego`` allows on the FIRST passing grant and simply ignores the rest, so
an extra credential on the wire costs nothing. A credential withheld costs a
working call, and the failure is invisible — it presents as the same "no grant
covers this call" this module exists to end.

That asymmetry decides every judgement call here. Nothing filters on the grant's
``credentialSubject.grant.tools`` allowlist: no policy reads it — helix
evaluates ``credentialSubject.constraints.rego`` keyed by grant id, which this
side cannot reproduce and should not try to. ``tools`` only ranks candidates
when the cap bites.

Selection mirrors the policy, and deliberately errs wide
--------------------------------------------------------

:func:`_covers` mirrors helix's ``structure_v2.rego``: some scope entry must
match the protocol, the message type and the resource. What this does NOT do is
decide anything the PDP decides — revocation and validity windows are checked
there, against sources this side cannot see. Attaching a revoked or expired
grant costs one denial; withholding one because a local cache thought it was
dead costs a working call, and that failure is silent.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote as url_quote

from .message import Attachment, AttachmentData

#: The most credentials put on one message.
#:
#: Over-attaching is free at the policy, but not on the wire: a holder with
#: per-tool grants can hold dozens, each a 1-2KB JWT, on every message. The cap
#: is far above any real holding; when it bites, the entries kept are the most
#: likely to matter (see :func:`select_for`) and the caller is TOLD, because a
#: credential dropped here produces the same indistinguishable denial as one
#: never held.
MAX_ATTACHED = 16

DEFAULT_GRANT_CACHE_MS = 60_000
DEFAULT_GRANT_READ_TIMEOUT_MS = 2_000

#: Reads the stored credential records for a holder DID. A callable rather than
#: a :class:`~layr8.rest.RestClient` so the cache and its failure TTL are
#: testable without a node.
CredentialReader = Callable[[str], Awaitable[list[dict[str, Any]]]]


@dataclass
class HeldCredential:
    """A grant this DID holds, decoded far enough to decide what it covers."""

    id: str
    raw_jwt: str
    scope: list[dict[str, Any]]
    #: Tool allowlist (``credentialSubject.grant.tools``). Empty ⇒ any tool.
    tools: list[str] = field(default_factory=list)
    #: The node's ``valid_until`` as epoch ms, when it sent one.
    #:
    #: NOT used to withhold anything — validity is the PDP's decision, made
    #: against a clock this side cannot see, and a skewed local clock dropping a
    #: live grant fails silently. It only breaks ties under :data:`MAX_ATTACHED`,
    #: so a live grant never loses its slot to one that has certainly lapsed.
    expires_at: float | None = None


def split_type_uri(type_uri: str) -> tuple[str, str]:
    """Split a DIDComm ``type`` into ``(protocol, message_type)``.

    A type URI is ``<protocol>/<messageType>`` and the policy matches the two
    separately. Splitting on the LAST slash is what the node's own parser does.
    """
    cut = type_uri.rfind("/")
    if cut <= 0:
        return type_uri, ""
    return type_uri[:cut], type_uri[cut + 1 :]


def tool_name_of(body: Any) -> str | None:
    """The tool name the policy will match, if this body carries one."""
    if not isinstance(body, dict):
        return None
    params = body.get("params")
    if not isinstance(params, dict):
        return None
    name = params.get("name")
    return name if isinstance(name, str) else None


# ── structure_v2.rego mirror ──


def _protocol_matches(scope_protocol: Any, want: str) -> bool:
    return scope_protocol == "*" or scope_protocol == want


def _message_type_matches(types: Any, want: str) -> bool:
    return isinstance(types, list) and ("*" in types or want in types)


def _resource_matches(resource: Any, want: str) -> bool:
    """The three ways a scope's ``resource`` can cover a message's.

    In the order ``structure_v2.rego``'s ``_resource_ok`` states them:

    1. equal;
    2. ``foo/*`` covers anything under ``foo/`` — note the rego strips only the
       ``*``, so the trailing slash is part of the prefix and ``foo/*`` does not
       cover ``foobar``;
    3. a bare ``foo`` covers ``foo/bar`` — a SEGMENT prefix, requiring the next
       character to be ``/``, so ``tables`` covers ``tables/customers`` but not
       ``tables_archive``.

    Clause 3 is the one that points the wrong way when it's missing: this side
    withholds a grant the policy would have honoured, which is the failure that
    costs a working call and shows up as "no grant covers this call".
    """
    if resource is None or resource == "" or resource == "*":
        return True
    if not isinstance(resource, str) or not isinstance(want, str):
        return False
    if resource.endswith("/*"):
        return want.startswith(resource[:-1])
    if resource == want:
        return True
    return want.startswith(resource) and want[len(resource) : len(resource) + 1] == "/"


def _covers(cred: HeldCredential, resource: str, protocol: str, message_type: str) -> bool:
    return any(
        _protocol_matches(s.get("protocol"), protocol)
        and _message_type_matches(s.get("messageTypes"), message_type)
        and _resource_matches(s.get("resource"), resource)
        for s in cred.scope
        if isinstance(s, dict)
    )


def select_for(
    creds: list[HeldCredential],
    *,
    recipients: list[str],
    type_uri: str,
    tool: str | None = None,
    now_ms: float | None = None,
    on_capped: Callable[[dict[str, int]], None] | None = None,
) -> list[Attachment]:
    """The covering set for one outbound message, as ready-to-send attachments.

    *recipients* is the message's ``to``: the node evaluates one decision per
    recipient, so a credential covering ANY of them belongs on the wire.

    An empty result is a legitimate outcome, not an error. Most DIDComm traffic
    — discovery, trust-ping, problem reports — rides the node's allow rules with
    no grant at all.

    *on_capped* is told when the cap left credentials off. Silence there is the
    same class of failure this module exists to end: the holder is the only
    party that knows a covering credential never reached the wire.
    """
    protocol, message_type = split_type_uri(type_uri)
    now = now_ms if now_ms is not None else time.time() * 1000

    covering = [
        c for c in creds if any(_covers(c, r, protocol, message_type) for r in recipients)
    ]

    # Ordering only matters when the cap bites. It decides which credentials are
    # LEFT OFF, so it ranks by how likely each one is to have been the one that
    # mattered — it is not a filter, and nothing here withholds anything the cap
    # has room for.
    #
    # Live beats lapsed by more than everything else combined: a certainly
    # expired grant cannot be the one that would have worked. Then the tool:
    # naming THIS tool first, naming no tool at all (unrestricted) second,
    # naming only OTHER tools last — last, not excluded, since `grant.tools` is
    # not a policy input anywhere. Named resource before wildcard as the
    # finest-grained tiebreak.
    def _rank(cred: HeldCredential) -> int:
        lapsed = 8 if cred.expires_at is not None and cred.expires_at <= now else 0
        if not cred.tools:
            tool_rank = 1
        elif tool is not None and tool in cred.tools:
            tool_rank = 0
        else:
            tool_rank = 2
        named = 0 if any(
            isinstance(s, dict)
            and isinstance(s.get("resource"), str)
            and s["resource"] not in ("", "*")
            and not s["resource"].endswith("/*")
            for s in cred.scope
        ) else 1
        return lapsed + tool_rank * 2 + named

    # Index as the tiebreak: a stable order, so the same message does not carry
    # a different set on each send.
    chosen = sorted(enumerate(covering), key=lambda pair: (_rank(pair[1]), pair[0]))
    chosen = chosen[:MAX_ATTACHED]

    if len(chosen) < len(covering) and on_capped is not None:
        on_capped({"covering": len(covering), "attached": len(chosen)})

    return [
        Attachment(
            id=cred.id,
            media_type="application/vc+jwt",
            data=AttachmentData(jws=cred.raw_jwt),
        )
        for _, cred in chosen
    ]


def _decode_jwt_payload(jwt: str) -> dict[str, Any]:
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


def _parse_valid_until(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return None


def parse_credential(rec: dict[str, Any]) -> HeldCredential | None:
    """Decode one stored credential record, or ``None`` if it is not a grant."""
    raw_jwt = rec.get("credential_jwt") or rec.get("raw_jwt") or rec.get("jwt")
    if not isinstance(raw_jwt, str):
        return None

    # A compact JWS has exactly three segments. Anything else cannot be verified
    # by the node, so putting it on the wire only costs a denial that names the
    # wrong problem.
    parts = raw_jwt.split(".")
    if len(parts) != 3 or not parts[2]:
        return None

    payload = _decode_jwt_payload(raw_jwt)
    # Claims are at the TOP LEVEL of the payload on this node; the `vc` wrapper
    # is the standard alternative and both are accepted.
    vc = payload["vc"] if isinstance(payload.get("vc"), dict) else payload
    cs = vc.get("credentialSubject")
    cs = cs if isinstance(cs, dict) else {}

    scope = cs.get("scope")
    # A VRTC has `grantable` instead of `scope` and belongs in the node's control
    # chain, not here. No scope, not a grant.
    if not isinstance(scope, list) or not scope:
        return None

    grant = cs.get("grant")
    grant = grant if isinstance(grant, dict) else {}
    tools = grant.get("tools")

    # `id` is what the REST contract calls it; `credential_id` is the column
    # name, accepted because the two have been confused at this boundary before.
    cred_id = vc.get("id") or payload.get("jti") or rec.get("id") or rec.get("credential_id")

    return HeldCredential(
        # The SIGNATURE segment as the fallback, not the head of the JWT: every
        # credential from one issuer shares a header, so the first bytes gave
        # them all the SAME attachment id — and a frame carrying two attachments
        # with one id is a frame whose second attachment may not survive.
        id=cred_id if isinstance(cred_id, str) and cred_id else f"urn:jws:{parts[2][:32]}",
        raw_jwt=raw_jwt,
        scope=scope,
        tools=tools if isinstance(tools, list) else [],
        expires_at=_parse_valid_until(
            rec.get("valid_until") or rec.get("validUntil") or vc.get("validUntil")
        ),
    )


class Wallet:
    """The grants a DID holds, read from the node and cached.

    Cached for *ttl_ms* because a send should not cost a round trip. The TTL is
    the whole freshness story: a grant minted seconds ago is invisible until it
    lapses, which is why it is short and why :meth:`refresh` exists for a caller
    that has just been told it was granted something.
    """

    def __init__(
        self,
        reader: CredentialReader,
        *,
        ttl_ms: float = DEFAULT_GRANT_CACHE_MS,
        read_timeout_ms: float = DEFAULT_GRANT_READ_TIMEOUT_MS,
        failure_ttl_ms: float | None = None,
    ) -> None:
        self._reader = reader
        self._ttl_ms = ttl_ms
        self._read_timeout_ms = read_timeout_ms
        # How long a FAILED read is remembered.
        #
        # A failure is cached at all because only caching successes meant an
        # agent whose API key cannot read credentials paid a full failing round
        # trip on EVERY outbound message, forever — turning a config mistake
        # into a permanent latency tax. Short, because the fix for that mistake
        # should take effect without a restart; never longer than a success is
        # cached, or lowering `ttl_ms` to see a new grant sooner would leave a
        # stale failure outliving it; and never shorter than the read deadline,
        # because the entry is stamped with the time the read STARTED, so a
        # shorter one is already lapsed the moment a timeout records it.
        self._failure_ttl_ms = (
            failure_ttl_ms
            if failure_ttl_ms is not None
            else min(ttl_ms, max(5_000, read_timeout_ms))
        )
        # did → (stamped_at_ms, credentials) or (stamped_at_ms, exception)
        self._cache: dict[str, tuple[float, list[HeldCredential] | Exception]] = {}

    def refresh(self, did: str | None = None) -> None:
        """Drop the cached grants for *did* (or all), forcing the next re-read."""
        if did is None:
            self._cache.clear()
        else:
            self._cache.pop(did, None)

    async def held_by(self, did: str, now_ms: float | None = None) -> list[HeldCredential]:
        """The grants *did* holds. Raises whatever the read raised."""
        now = now_ms if now_ms is not None else time.monotonic() * 1000

        hit = self._cache.get(did)
        if hit is not None:
            stamped, value = hit
            ttl = self._failure_ttl_ms if isinstance(value, Exception) else self._ttl_ms
            if now - stamped < ttl:
                if isinstance(value, Exception):
                    raise value
                return value

        try:
            records = await self._reader(did)
        except Exception as exc:
            self._cache[did] = (now, exc)
            raise

        creds = [c for c in (parse_credential(r) for r in records) if c is not None]
        self._cache[did] = (now, creds)
        return creds

    async def attachments_for(
        self,
        did: str,
        *,
        recipients: list[str],
        type_uri: str,
        body: Any = None,
        on_capped: Callable[[dict[str, int]], None] | None = None,
    ) -> list[Attachment]:
        """The attachments for one outbound message, or ``[]`` if nothing covers it."""
        creds = await self.held_by(did)
        return select_for(
            creds,
            recipients=recipients,
            type_uri=type_uri,
            tool=tool_name_of(body),
            on_capped=on_capped,
        )


def rest_credential_reader(rest: Any, read_timeout_ms: float) -> CredentialReader:
    """A :data:`CredentialReader` over the SDK's REST client.

    The REST client rather than a bare ``aiohttp`` session, deliberately: it
    carries the ``*.localhost`` resolver that makes local development work, and
    the ``x-api-key`` header.
    """

    async def read(did: str) -> list[dict[str, Any]]:
        path = "/api/v1/credentials?holder_did=" + url_quote(did, safe="")
        # The deadline is the reason a hung node cannot stall every send behind
        # it. It belongs on the request itself: a race around the call would
        # leave the connection open and the read still running.
        result = await rest.get(path, timeout_ms=read_timeout_ms)
        if isinstance(result, list):
            return result
        creds = result.get("credentials") if isinstance(result, dict) else None
        return creds if isinstance(creds, list) else []

    return read
