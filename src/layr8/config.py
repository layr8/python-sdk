"""Configuration for the Layr8 SDK."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .errors import Layr8Error

#: Default grant cache TTL. Short: a grant minted seconds ago is invisible until
#: it lapses.
DEFAULT_GRANT_CACHE_MS = 60_000

#: Default deadline on the credential read.
#:
#: Two seconds, chosen from both ends. It is a JSON GET to the same node this
#: client already holds a WebSocket to, so the honest answer arrives in
#: milliseconds — two seconds survives a cold node, a warming connection pool
#: and a GC pause. And it sits comfortably under the wallet's 5s failure cache,
#: which is measured from the START of the read; a deadline at or above that TTL
#: would leave the failure lapsed the moment it was recorded, so every send
#: would pay the full deadline instead of one per window.
DEFAULT_GRANT_READ_TIMEOUT_MS = 2_000

#: Default deadline on every other REST call.
#:
#: Thirty seconds, and the number matters less than what it is measured on: this
#: is a deadline on the whole call, so the node's own signing time counts
#: against it. 30s is far above any honest sign against a node this client
#: already holds a WebSocket to, and far below any duration a person watching
#: would still call "working" rather than "hung".
DEFAULT_REST_TIMEOUT_MS = 30_000


@dataclass
class GrantMissInfo:
    """What ``Config.on_grant_miss`` is told.

    Exactly one of *denial_code*, *capped* and *error* is set, and which one
    says what happened:

    - *denial_code* — the node denied a message we sent with **nothing
      attached**. This is the case the callback exists for.
    - *capped* — more grants covered the message than fit on it, as
      ``{"covering": n, "attached": 16}``. The policy allows on the first
      passing grant, so the one that mattered may be among those left off.
    - *error* — the grants could not be **read** at all. Never a normal outcome:
      every send after it is flying blind.
    """

    to: list[str] = field(default_factory=list)
    type: str = ""
    denial_code: str | None = None
    capped: dict[str, int] | None = None
    error: BaseException | None = None


@dataclass
class Config:
    """
    Configuration for a Layr8 client.

    All fields fall back to environment variables if empty:
      - node_url  -> LAYR8_NODE_URL (required)
      - api_key   -> LAYR8_API_KEY (required)
      - agent_did -> LAYR8_AGENT_DID (optional)
      - protocols -> additional protocol URIs to advertise on join
      - attach_grants -> LAYR8_ATTACH_GRANTS ("false"/"0" turns it off)
      - grant_cache_ms -> LAYR8_GRANT_CACHE_MS
      - grant_read_timeout_ms -> LAYR8_GRANT_READ_TIMEOUT_MS
      - rest_timeout_ms -> LAYR8_REST_TIMEOUT_MS
      - mediator -> LAYR8_MEDIATOR_DID
      - mediator_live -> LAYR8_MEDIATOR_LIVE (default true)
      - didcomm_url -> LAYR8_DIDCOMM_URL (default <rest url>/didcomm)
    """

    node_url: str = ""
    api_key: str = ""
    agent_did: str = ""
    protocols: list[str] | None = None

    #: Attach the Verifiable Grants covering each outbound message. Default
    #: ``True``.
    #:
    #: The node requires a grant for anything its policy does not allow
    #: outright. Before this existed nothing in this SDK attached one — which is
    #: what produced "no grant covers this call" denials that read as a
    #: misconfigured grant rather than an absent one. On by default: opting IN
    #: would have left every existing agent in exactly the state that cost two
    #: teams days.
    attach_grants: bool | None = None
    #: How long held grants are cached before re-reading. Default 60s.
    grant_cache_ms: float | None = None
    #: Deadline on the credential read. Default 2s.
    #:
    #: The read sits in front of every send, so an unbounded one against a node
    #: that accepted the connection and went quiet stalls the send itself. A
    #: lapsed deadline is an ordinary read error: the message goes out
    #: unattached and *on_grant_miss* says so.
    grant_read_timeout_ms: float | None = None
    #: Deadline on every other REST call. Default 30s; ``0`` for none.
    rest_timeout_ms: float | None = None
    #: Called when a message went out with NO covering grant and the node then
    #: denied it, when the covering set had to be capped, or when the grants
    #: could not be read. See :class:`GrantMissInfo`.
    #:
    #: Wire this to a log. The node's denial names the grant it could not find,
    #: which sends people to check a grant that is fine; only the sender knows
    #: no credential was ever on the wire.
    on_grant_miss: Callable[[GrantMissInfo], Any] | None = None
    #: A mediator DID (see :mod:`layr8.mediation`): on every (re)connect the
    #: client enrols, declares it on the node, collects what was queued while
    #: offline and turns live delivery on. Fallback: ``LAYR8_MEDIATOR_DID``.
    mediator: str | None = None
    #: ``False`` collects but leaves live delivery off. Fallback:
    #: ``LAYR8_MEDIATOR_LIVE`` (default ``True``).
    mediator_live: bool | None = None
    #: Where collected ciphertext is re-injected. Fallback:
    #: ``LAYR8_DIDCOMM_URL``; default ``<rest url>/didcomm``.
    didcomm_url: str | None = None


@dataclass(frozen=True)
class ResolvedConfig:
    """Resolved configuration with required fields guaranteed present."""

    node_url: str
    api_key: str
    agent_did: str
    protocols: list[str]
    attach_grants: bool = True
    grant_cache_ms: float = DEFAULT_GRANT_CACHE_MS
    grant_read_timeout_ms: float = DEFAULT_GRANT_READ_TIMEOUT_MS
    rest_timeout_ms: float = DEFAULT_REST_TIMEOUT_MS
    mediator: str | None = None
    mediator_live: bool = True
    didcomm_url: str | None = None


def resolve_config(cfg: Config) -> ResolvedConfig:
    """Fill empty fields from environment variables and validate required fields."""
    node_url = cfg.node_url or os.environ.get("LAYR8_NODE_URL", "")
    api_key = cfg.api_key or os.environ.get("LAYR8_API_KEY", "")
    agent_did = cfg.agent_did or os.environ.get("LAYR8_AGENT_DID", "")

    if not node_url:
        raise Layr8Error(
            "node_url is required (set in Config or LAYR8_NODE_URL env)"
        )

    # Normalize HTTP(S) URLs to WebSocket scheme.
    # In production, the /plugin_socket endpoint serves WebSocket over HTTPS.
    if node_url.startswith("https://"):
        node_url = "wss://" + node_url.removeprefix("https://")
    elif node_url.startswith("http://"):
        node_url = "ws://" + node_url.removeprefix("http://")

    if not api_key:
        raise Layr8Error(
            "api_key is required (set in Config or LAYR8_API_KEY env)"
        )

    protocols = list(cfg.protocols) if cfg.protocols else []

    return ResolvedConfig(
        node_url=node_url,
        api_key=api_key,
        agent_did=agent_did,
        protocols=protocols,
        attach_grants=_resolve_bool(
            cfg.attach_grants, os.environ.get("LAYR8_ATTACH_GRANTS"), True
        ),
        grant_cache_ms=_resolve_ms(
            cfg.grant_cache_ms,
            os.environ.get("LAYR8_GRANT_CACHE_MS"),
            DEFAULT_GRANT_CACHE_MS,
        ),
        # Zero is not accepted here, unlike the cache TTL where it means "never
        # cache": a zero deadline would abort every read before it started,
        # turning a mistyped variable into an agent that attaches nothing at all
        # — the exact failure this whole feature exists to end.
        grant_read_timeout_ms=_resolve_ms(
            cfg.grant_read_timeout_ms,
            os.environ.get("LAYR8_GRANT_READ_TIMEOUT_MS"),
            DEFAULT_GRANT_READ_TIMEOUT_MS,
            minimum=1,
        ),
        # Zero IS accepted here, and the contrast with the line above is
        # deliberate. On the grant read a zero deadline silently attaches
        # nothing; on these calls it means "no deadline", which is the
        # pre-existing behaviour and a legitimate thing for an operator with a
        # slow node to ask for.
        rest_timeout_ms=_resolve_ms(
            cfg.rest_timeout_ms,
            os.environ.get("LAYR8_REST_TIMEOUT_MS"),
            DEFAULT_REST_TIMEOUT_MS,
        ),
        mediator=_blank_to_none(cfg.mediator or os.environ.get("LAYR8_MEDIATOR_DID")),
        mediator_live=_resolve_bool(
            cfg.mediator_live, os.environ.get("LAYR8_MEDIATOR_LIVE"), True
        ),
        didcomm_url=_blank_to_none(cfg.didcomm_url or os.environ.get("LAYR8_DIDCOMM_URL")),
    )


def _blank_to_none(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _resolve_bool(explicit: bool | None, raw: str | None, fallback: bool) -> bool:
    """Env booleans, spelled the way operators spell them.

    Anything unrecognised — including the empty string an unset-but-exported
    variable produces — leaves the default alone rather than reading as False.
    """
    if explicit is not None:
        return explicit
    if raw is None:
        return fallback
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return fallback


def _resolve_ms(
    explicit: float | None,
    raw: str | None,
    fallback: float,
    *,
    minimum: float = 0,
) -> float:
    """A millisecond setting, from the explicit config or the environment.

    A non-numeric or out-of-range value is IGNORED rather than turned into a
    ``nan`` that fails every comparison downstream and silently leaves the call
    unbounded. *minimum* binds the EXPLICIT value too, not just the env one —
    the caller is where a bad value actually comes from::

        Config(rest_timeout_ms=float(os.environ.get("MY_TIMEOUT", "nan")))
    """
    if explicit is not None:
        value = float(explicit)
        return value if value == value and value >= minimum else fallback  # NaN != NaN
    if raw is None or not raw.strip():
        return fallback
    try:
        value = float(raw)
    except ValueError:
        return fallback
    return value if value == value and value >= minimum else fallback
