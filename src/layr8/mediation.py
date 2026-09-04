"""
Store-and-forward for an agent that is not always connected, against a
DIDComm mediator (``layr8/mediator``): coordinate-mediation/3.0 enrolment,
messagepickup/3.0 collection, and the cloud-node's mediator declaration.

The cloud-node deposits any message that arrives while this agent's plugin
is offline with the mediator the agent has **declared** on its node
(``PUT /api/v1/dids/:did/mediator``, cloud-node ADR 0005). What the mediator
holds is the original ciphertext, so collecting it means posting each
attachment back to this agent's own node at ``/didcomm``, where it is
unpacked, sender-bound and authorized exactly like a first arrival, and then
delivered to this client's handlers. Nothing here ever decrypts.

Zero-config use — give the client a mediator and it does the rest on every
(re)connect, in the background::

    Client(Config(mediator="did:web:node.example:mediator"), log_errors())

(or ``LAYR8_MEDIATOR_DID``). Steps: :func:`enroll` → :func:`declare` →
:func:`pickup` → :func:`live`. Every step is also callable by hand; none
raises for a remote refusal — they return results with ``ok=False`` — only
programming errors raise.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import ProblemReportError
from .message import Attachment, Message
from .rest import post_didcomm

if TYPE_CHECKING:
    from .client import Client
    from .handler import HandlerFn

CM = "https://didcomm.org/coordinate-mediation/3.0/"
PICKUP = "https://didcomm.org/messagepickup/3.0/"

#: The message type a mediated client handles for live pushes.
DELIVERY_TYPE = f"{PICKUP}delivery"

#: Protocol bases a mediated client subscribes to.
MEDIATION_PROTOCOLS = [CM[:-1], PICKUP[:-1]]

DEFAULT_LIMIT = 10
MAX_ROUNDS = 100
DEFAULT_TIMEOUT = 20.0

#: A failed step's error: a string, or the mediator's problem report.
MediationError = str | ProblemReportError


@dataclass
class SimpleResult:
    ok: bool
    error: MediationError | None = None


@dataclass
class EnrollResult:
    ok: bool
    routing_did: list[str] = field(default_factory=list)
    updated: list[dict[str, Any]] = field(default_factory=list)
    error: MediationError | None = None


@dataclass
class PickupResult:
    ok: bool
    collected: int = 0
    error: MediationError | None = None


@dataclass
class StatusResult:
    ok: bool
    status: dict[str, Any] = field(default_factory=dict)
    error: MediationError | None = None


@dataclass
class BootstrapResult:
    ok: bool
    collected: int = 0
    #: The step that failed: ``enroll`` | ``declare`` | ``pickup`` | ``live``.
    step: str = ""
    error: MediationError | None = None


@dataclass
class ReinjectResult:
    ok: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


@dataclass
class CollectResult:
    collected: int = 0
    #: False when something was left with the mediator.
    complete: bool = True


def _fail_error(err: BaseException) -> MediationError:
    if isinstance(err, ProblemReportError):
        return err
    return str(err) or err.__class__.__name__


async def _request(
    client: Client,
    mediator: str,
    msg_type: str,
    body: Any,
    timeout: float,
) -> Message:
    return await client.request(
        Message(type=msg_type, to=[mediator], body=body), timeout=timeout
    )


def _body(msg: Message) -> dict[str, Any]:
    body = msg.unmarshal_body()
    return body if isinstance(body, dict) else {}


def own_registered(updated: Any, own: str) -> bool:
    """Whether the agent's own DID came back registered (success or no_change)."""
    if not isinstance(updated, list):
        return False
    for u in updated:
        if isinstance(u, dict) and u.get("recipient_did") == own:
            return u.get("result") in ("success", "no_change")
    return False


def ciphertext(att: Attachment) -> bytes | None:
    """The attachment's ciphertext (base64url ``data.base64``), or None."""
    b64 = att.data.base64
    if not isinstance(b64, str) or b64 == "":
        return None
    try:
        std = b64.translate(str.maketrans("-_", "+/")) + "=" * (-len(b64) % 4)
        raw = base64.b64decode(std, validate=True)
    except (ValueError, TypeError):
        return None
    return raw or None


def mediator_path(did: str) -> str:
    return f"/api/v1/dids/{did}/mediator"


async def enroll(
    client: Client,
    mediator: str,
    *,
    recipients: list[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> EnrollResult:
    """
    Request mediation and register this agent's DID (plus *recipients*) with
    *mediator*. Idempotent: a second call re-receives the grant and gets
    ``no_change`` on the registrations.
    """
    try:
        grant = await _request(client, mediator, f"{CM}mediate-request", {}, timeout)
        if grant.type == f"{CM}mediate-deny":
            return EnrollResult(ok=False, error="mediate_denied")
        if grant.type != f"{CM}mediate-grant":
            return EnrollResult(ok=False, error=f"unexpected reply {grant.type}")
        routing = _body(grant).get("routing_did")
        if isinstance(routing, list):
            routing_did = [str(r) for r in routing]
        elif routing:
            routing_did = [str(routing)]
        else:
            routing_did = []

        own = client.did
        wanted = list(dict.fromkeys([own, *(recipients or [])]))
        updates = [{"recipient_did": d, "action": "add"} for d in wanted]
        resp = await _request(
            client, mediator, f"{CM}recipient-update", {"updates": updates}, timeout
        )
        if resp.type != f"{CM}recipient-update-response":
            return EnrollResult(ok=False, error=f"unexpected reply {resp.type}")
        updated = _body(resp).get("updated") or []
        if not own_registered(updated, own):
            return EnrollResult(
                ok=False,
                error=f"recipient-update did not register {own}: {updated!r}",
            )
        return EnrollResult(ok=True, routing_did=routing_did, updated=list(updated))
    except Exception as err:  # noqa: BLE001 — every remote refusal becomes a result
        return EnrollResult(ok=False, error=_fail_error(err))


async def declare(client: Client, mediator: str) -> SimpleResult:
    """
    Declare *mediator* as this agent's mediator on its own cloud-node, so the
    node deposits messages there while the agent is offline and the DID
    document advertises it as ``routingKeys``.
    """
    try:
        await client._rest.put(mediator_path(client.did), {"routing_did": mediator})
        return SimpleResult(ok=True)
    except Exception as err:  # noqa: BLE001
        return SimpleResult(ok=False, error=_fail_error(err))


async def undeclare(client: Client) -> SimpleResult:
    """Remove this agent's mediator declaration on its node."""
    try:
        await client._rest.delete(mediator_path(client.did))
        return SimpleResult(ok=True)
    except Exception as err:  # noqa: BLE001
        return SimpleResult(ok=False, error=_fail_error(err))


async def reinject(
    client: Client,
    attachments: list[Attachment],
    *,
    didcomm_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> ReinjectResult:
    """
    Post each attachment's ciphertext to this agent's node at ``/didcomm``.
    Returns the ids that went in and the ids that did not (those are never
    acknowledged, so the mediator keeps them).
    """
    url = didcomm_url or client.didcomm_url
    result = ReinjectResult()
    for att in attachments:
        jwe = ciphertext(att)
        if jwe is None:
            result.failed.append(att.id)
            continue
        try:
            await post_didcomm(url, jwe, timeout_ms=timeout * 1000)
            result.ok.append(att.id)
        except Exception:  # noqa: BLE001
            result.failed.append(att.id)
    return result


async def collect(
    client: Client,
    mediator: str,
    attachments: list[Attachment],
    *,
    didcomm_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> CollectResult:
    """Re-inject *attachments* and acknowledge the ones that went in."""
    r = await reinject(client, attachments, didcomm_url=didcomm_url, timeout=timeout)
    if r.ok:
        try:
            await _request(
                client,
                mediator,
                f"{PICKUP}messages-received",
                {"message_id_list": r.ok},
                timeout,
            )
        except Exception:  # noqa: BLE001
            # The messages are in; a lost ack only means a redelivery next time.
            pass
    return CollectResult(collected=len(r.ok), complete=not r.failed)


async def pickup(
    client: Client,
    mediator: str,
    *,
    limit: int = DEFAULT_LIMIT,
    didcomm_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> PickupResult:
    """
    Drain the mediator: repeat ``delivery-request`` until a ``status`` reply,
    re-injecting and acknowledging as it goes. Stops early, without losing
    anything, if a re-injection fails.
    """
    collected = 0
    try:
        for _ in range(MAX_ROUNDS):
            reply = await _request(
                client, mediator, f"{PICKUP}delivery-request", {"limit": limit}, timeout
            )
            if reply.type == f"{PICKUP}status":
                break
            if reply.type != DELIVERY_TYPE:
                return PickupResult(ok=False, error=f"unexpected reply {reply.type}")
            if not reply.attachments:
                break
            r = await collect(
                client,
                mediator,
                reply.attachments,
                didcomm_url=didcomm_url,
                timeout=timeout,
            )
            collected += r.collected
            if not r.complete:
                break
        return PickupResult(ok=True, collected=collected)
    except Exception as err:  # noqa: BLE001
        return PickupResult(ok=False, collected=collected, error=_fail_error(err))


async def live(
    client: Client,
    mediator: str,
    flag: bool,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> StatusResult:
    """Turn live delivery on or off; returns the mediator's ``status`` body."""
    try:
        reply = await _request(
            client,
            mediator,
            f"{PICKUP}live-delivery-change",
            {"live_delivery": flag},
            timeout,
        )
        if reply.type != f"{PICKUP}status":
            return StatusResult(ok=False, error=f"unexpected reply {reply.type}")
        return StatusResult(ok=True, status=_body(reply))
    except Exception as err:  # noqa: BLE001
        return StatusResult(ok=False, error=_fail_error(err))


async def status(
    client: Client, mediator: str, *, timeout: float = DEFAULT_TIMEOUT
) -> StatusResult:
    """Ask the mediator how many messages are waiting."""
    try:
        reply = await _request(client, mediator, f"{PICKUP}status-request", {}, timeout)
        if reply.type != f"{PICKUP}status":
            return StatusResult(ok=False, error=f"unexpected reply {reply.type}")
        return StatusResult(ok=True, status=_body(reply))
    except Exception as err:  # noqa: BLE001
        return StatusResult(ok=False, error=_fail_error(err))


def delivery_handler(client: Client) -> HandlerFn:
    """
    The handler a mediated client registers for the mediator's live
    ``delivery`` pushes: re-inject and acknowledge, no reply.
    """

    async def handle(msg: Message) -> None:
        await collect(client, msg.from_, list(msg.attachments))
        return None

    return handle


async def bootstrap(
    client: Client,
    mediator: str,
    *,
    recipients: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    didcomm_url: str | None = None,
    live_delivery: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
) -> BootstrapResult:
    """
    :func:`enroll` → :func:`declare` → :func:`pickup` → :func:`live` (when
    *live_delivery*), stopping at the first failure. Never raises.
    """
    e = await enroll(client, mediator, recipients=recipients, timeout=timeout)
    if not e.ok:
        return BootstrapResult(ok=False, step="enroll", error=e.error)
    d = await declare(client, mediator)
    if not d.ok:
        return BootstrapResult(ok=False, step="declare", error=d.error)
    p = await pickup(client, mediator, limit=limit, didcomm_url=didcomm_url, timeout=timeout)
    if not p.ok:
        return BootstrapResult(ok=False, step="pickup", collected=p.collected, error=p.error)
    if live_delivery:
        lv = await live(client, mediator, True, timeout=timeout)
        if not lv.ok:
            return BootstrapResult(ok=False, step="live", collected=p.collected, error=lv.error)
    return BootstrapResult(ok=True, collected=p.collected)
