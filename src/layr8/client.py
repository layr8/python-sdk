"""Layr8 DIDComm Agent Client."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from urllib.parse import quote as url_quote

from .channel import PhoenixChannel
from .config import Config, GrantMissInfo, resolve_config
from .credentials import Credential, StoredCredential, VerifiedCredential
from .errors import (
    AlreadyConnectedError,
    ClientClosedError,
    ErrorHandler,
    ErrorKind,
    NotConnectedError,
    ProblemReportError,
    SDKError,
    ServerRejectError,
)
from .handler import HandlerEntry, HandlerFn, HandlerRegistry
from .identity import is_identity_attachment
from .mcp import DEFAULT_MCP_BASE, McpBinding
from .message import Message, generate_id, marshal_didcomm, parse_didcomm
from .sentinel import _Pass
from .presentations import VerifiedPresentation
from .rest import RestClient, rest_url_from_websocket
from .wallet import Wallet, rest_credential_reader

_PROBLEM_REPORT_TYPE = "https://didcomm.org/report-problem/2.0/problem-report"

#: How long a message sent with nothing attached is kept, waiting for a denial.
#:
#: The node evaluates before it delivers, so the denial follows its message by a
#: round trip — one second would cover it. A minute is chosen to survive a paused
#: process or a reconnect, and it is what bounds the map: see
#: ``Client._remember_unattached``.
_UNATTACHED_WINDOW_MS = 60_000


class Client:
    """
    Main entry point for interacting with the Layr8 platform.

    Lifecycle::

        Client(Config, on_error) → handle (register handlers) → connect → ... → close

    Or using the async context manager::

        Client(Config, on_error) → handle → async with client: ...

    The *on_error* callback is **required**.  It receives an :class:`SDKError`
    for every SDK-level error (parse failures, missing handlers, handler
    exceptions, server rejects, transport write errors).  Use
    :func:`log_errors` for a convenient default::

        from layr8 import Client, Config, log_errors
        client = Client(Config(...), on_error=log_errors())
    """

    def __init__(self, config: Config, on_error: ErrorHandler) -> None:
        if not callable(on_error):
            raise TypeError(
                "on_error is required: pass log_errors() or a custom Callable[[SDKError], None]"
            )
        self._on_error = on_error

        self._cfg = resolve_config(config)
        self._registry = HandlerRegistry()
        self._channel: PhoenixChannel | None = None
        self._connected = False
        self._closed = False
        self._agent_did = self._cfg.agent_did

        # REST client for credential / presentation APIs
        rest_url = rest_url_from_websocket(self._cfg.node_url)
        self._rest = RestClient(rest_url, self._cfg.api_key, self._cfg.rest_timeout_ms)

        # On by default. A grant the node requires and the SDK does not attach
        # is indistinguishable, from the caller's side, from a grant that was
        # never issued — and the denial names the grant, not the omission.
        self._wallet: Wallet | None = (
            Wallet(
                rest_credential_reader(self._rest, self._cfg.grant_read_timeout_ms),
                ttl_ms=self._cfg.grant_cache_ms,
                read_timeout_ms=self._cfg.grant_read_timeout_ms,
            )
            if self._cfg.attach_grants
            else None
        )
        self._on_grant_miss = config.on_grant_miss
        # Messages that went out with nothing attached, keyed by thread, so a
        # denial can be matched back to them. Bounded by AGE — a diagnostic, not
        # a ledger. See _remember_unattached.
        self._unattached: dict[str, tuple[float, list[str], str]] = {}
        # Outbound writes happen in CALL order, whatever the wallet does: the
        # grant read puts an await in front of every send, so without this two
        # sends issued back to back could arrive reversed when the first one's
        # read is the slower. Agents that emit a sequence without awaiting each
        # call are entitled to their order, and a public SDK does not get to
        # change that quietly.
        #
        # It covers the grant read and the marshal, and deliberately NOT the
        # send itself. `PhoenixChannel.send` waits up to 15s for the server's
        # ack; holding the lock across that made one slow ack block every other
        # send and every handler reply behind it — head-of-line blocking this
        # client never had. The read is the only new suspension point and so the
        # only thing that can reorder; once it is done the write follows in the
        # same task step, because releasing an asyncio lock does not yield.
        self._write_lock = asyncio.Lock()
        # MCP protocol bases already subscribed via mcp() (idempotency guard)
        self._mcp_bases: set[str] = set()

        # Correlation map for Request/Response: thread_id → Future
        self._pending: dict[str, asyncio.Future[Message]] = {}

        # Disconnect / reconnect callbacks
        self._disconnect_fn: Callable[[Exception], None] | None = None
        self._reconnect_fn: Callable[[], None] | None = None

    @property
    def did(self) -> str:
        """The agent's DID — either provided in Config or assigned by the node."""
        return self._agent_did

    def handle(
        self,
        msg_type: str,
        fn: HandlerFn | None = None,
    ) -> Callable[[HandlerFn], HandlerFn] | None:
        """
        Register a handler for a DIDComm message type.

        Can be used as a decorator::

            @client.handle("https://layr8.io/protocols/echo/1.0/request")
            async def echo(msg: Message) -> Message:
                ...

        Or called directly::

            client.handle("https://layr8.io/protocols/echo/1.0/request", echo_fn)

        Must be called BEFORE ``connect()``.
        """
        if self._connected:
            raise AlreadyConnectedError()

        if fn is not None:
            self._registry.register(msg_type, fn)
            return None

        # Decorator mode
        def decorator(handler: HandlerFn) -> HandlerFn:
            self._registry.register(msg_type, handler)
            return handler

        return decorator

    def handle_all(
        self,
        fn: HandlerFn | None = None,
    ) -> Callable[[HandlerFn], HandlerFn] | None:
        """
        Register a catch-all handler for any unhandled message type.

        Can be used as a decorator::

            @client.handle_all
            async def catch_all(msg: Message) -> Message | None:
                ...

        Or called directly::

            client.handle_all(my_fn)

        Must be called BEFORE ``connect()``.
        """
        if self._connected:
            raise AlreadyConnectedError()

        if fn is not None:
            self._registry.register_catch_all(fn)
            return None

        # Decorator mode
        def decorator(handler: HandlerFn) -> HandlerFn:
            self._registry.register_catch_all(handler)
            return handler

        return decorator

    def mcp(self, base: str = DEFAULT_MCP_BASE) -> McpBinding:
        """Set up MCP (Model Context Protocol) over DIDComm on a protocol *base*.

        Returns a binding whose ``peer(did)`` yields a caller with
        ``initialize()``, ``list_tools()`` and ``call_tool()``.

        A peer's MCP surface is DIDComm request/reply: a request of type
        ``{base}/<method>`` with a JSON-RPC body, answered by a
        ``{base}/<method>-result`` message. The reply echoes the request's
        ``thid``, so ``request()`` correlates it — this binding removes the
        boilerplate.

        Must be called BEFORE ``connect()`` (like ``handle()``): it registers
        the protocol subscription the cloud-node needs in order to deliver
        ``{base}/*`` replies. Idempotent per base.
        """
        if self._connected:
            raise AlreadyConnectedError()
        if self._closed:
            raise ClientClosedError()

        if base not in self._mcp_bases:
            # A no-op handler whose type derives the `base` protocol subscribes
            # the client to it (see HandlerRegistry.protocols). Correlated
            # replies are consumed in _handle_inbound_message BEFORE handler
            # lookup, so this handler only ever fires for an *uncorrelated*
            # `{base}/…` message (none in normal request/reply use) — passing is
            # the safe default.
            async def _noop(_msg: Message) -> Any:
                return _Pass()

            self._registry.register(f"{base}/_mcp", _noop)
            self._mcp_bases.add(base)

        return McpBinding(self, base)

    def refresh_grants(self, did: str | None = None) -> None:
        """Forget the cached grants for *did* (default: this agent's).

        The cache TTL is the whole freshness story: a grant minted seconds ago
        is invisible until it lapses. An agent that has just been TOLD it was
        granted something — by a request/approve flow, or by a person on the
        other end of a chat — should not have to wait out a timer it cannot see.
        """
        if self._wallet is not None:
            self._wallet.refresh(did or self._agent_did)

    async def connect(self) -> None:
        """Establish WebSocket connection and join the Phoenix Channel."""
        if self._connected:
            raise AlreadyConnectedError()
        if self._closed:
            raise ClientClosedError()

        protocols = list(dict.fromkeys(
            self._cfg.protocols + self._registry.protocols()
        ))

        channel = PhoenixChannel(
            self._cfg.node_url,
            self._cfg.api_key,
            self._cfg.agent_did,
            on_message=self._handle_inbound_message,
            on_disconnect=self._on_disconnect,
            on_reconnect=self._reconnect_fn,
        )

        await channel.connect(protocols)

        if not self._agent_did and channel.assigned_did:
            self._agent_did = channel.assigned_did

        self._channel = channel
        self._connected = True

    async def close(self) -> None:
        """Gracefully shut down the client connection."""
        if self._closed:
            return
        self._closed = True
        self._connected = False

        if self._channel:
            await self._channel.close()
            self._channel = None

        # Close the REST client session
        await self._rest.close()

        # Cancel all pending requests
        for thread_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.cancel()
            del self._pending[thread_id]

    async def __aenter__(self) -> Client:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def on_disconnect(self, fn: Callable[[Exception], None]) -> None:
        """Register a callback for unexpected disconnection."""
        self._disconnect_fn = fn

    def on_reconnect(self, fn: Callable[[], None]) -> None:
        """Register a callback for reconnection."""
        self._reconnect_fn = fn

    async def send(self, msg: Message, *, fire_and_forget: bool = False) -> None:
        """
        Send a message.

        By default, waits for the server to acknowledge the message.
        Pass ``fire_and_forget=True`` to skip server acknowledgment.
        """
        if not self._connected or not self._channel:
            raise NotConnectedError()

        self._fill_message(msg)

        if fire_and_forget:
            await self._send_message_fire_and_forget(msg)
        else:
            await self._send_message_acked(msg)

    async def request(
        self,
        msg: Message,
        *,
        parent_thread: str = "",
        timeout: float = 30.0,
    ) -> Message:
        """
        Send a message and wait for a correlated response.

        Raises ``asyncio.TimeoutError`` on timeout, ``ProblemReportError``
        if the remote handler returned an error.
        """
        if not self._connected or not self._channel:
            raise NotConnectedError()

        self._fill_message(msg)
        if not msg.thread_id:
            msg.thread_id = generate_id()
        if parent_thread:
            msg.parent_thread_id = parent_thread

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Message] = loop.create_future()
        self._pending[msg.thread_id] = future

        try:
            await self._send_message_acked(msg)
            resp = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg.thread_id, None)
            raise
        except Exception:
            self._pending.pop(msg.thread_id, None)
            raise

        # Check if response is a problem report
        if resp.type == "https://didcomm.org/report-problem/2.0/problem-report":
            body = resp.unmarshal_body()
            raise ProblemReportError(
                code=body.get("code", "unknown") if isinstance(body, dict) else "unknown",
                comment=body.get("comment", "unknown error") if isinstance(body, dict) else "unknown error",
            )

        return resp

    def _handle_inbound_message(self, payload: Any) -> None:
        """Called by the channel for each inbound 'message' event."""
        try:
            msg = parse_didcomm(payload)
        except Exception as exc:
            self._on_error(SDKError(
                kind=ErrorKind.PARSE_FAILURE,
                cause=exc,
                raw=payload,
            ))
            return

        # Before routing, not after: a denial that resolves a pending request is
        # handed to the waiter and never reaches a handler, and a denial with no
        # waiter goes to one — _note_denial has to see both.
        self._note_denial(msg)

        # Check if this is a response to a pending Request (by thread ID)
        if msg.thread_id and msg.thread_id in self._pending:
            future = self._pending.pop(msg.thread_id)
            if not future.done():
                future.set_result(msg)
            return

        if self._channel and self._channel.reply_protocol:
            self._dispatch_new_mode(msg)
        else:
            self._dispatch_legacy_mode(msg)

    def _dispatch_new_mode(self, msg: Message) -> None:
        """Dispatch using reply protocol — send dispatch_reply after handler."""
        entry = self._registry.lookup(msg.type)
        if not entry:
            self._on_error(SDKError(
                kind=ErrorKind.NO_HANDLER,
                message_id=msg.id,
                type=msg.type,
                from_did=msg.from_,
            ))
            asyncio.ensure_future(self._send_dispatch_reply(msg.id, "pass"))
            return

        asyncio.ensure_future(self._run_handler_new_mode(entry, msg))

    def _dispatch_legacy_mode(self, msg: Message) -> None:
        """Dispatch using legacy ack protocol."""
        entry = self._registry.lookup(msg.type)
        if not entry:
            self._on_error(SDKError(
                kind=ErrorKind.NO_HANDLER,
                message_id=msg.id,
                type=msg.type,
                from_did=msg.from_,
            ))
            return

        # Auto-ack before handler
        if self._channel:
            task = asyncio.ensure_future(self._channel.send_ack([msg.id]))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

        asyncio.ensure_future(self._run_handler(entry, msg))

    async def _run_handler(self, entry: HandlerEntry, msg: Message) -> None:
        """Execute a handler and send back the response or problem report (legacy mode)."""
        try:
            resp = await entry.fn(msg)
        except Exception as exc:
            self._on_error(SDKError(
                kind=ErrorKind.HANDLER_EXCEPTION,
                message_id=msg.id,
                type=msg.type,
                from_did=msg.from_,
                cause=exc,
            ))
            try:
                await self._send_problem_report(msg, exc)
            except Exception:
                pass
            return

        if resp is not None and isinstance(resp, Message):
            self._fill_response(resp, msg)
            try:
                await self._send_message(resp)
            except Exception as exc:
                self._on_error(SDKError(
                    kind=ErrorKind.TRANSPORT_WRITE,
                    message_id=msg.id,
                    type=msg.type,
                    from_did=msg.from_,
                    cause=exc,
                ))

    async def _run_handler_new_mode(self, entry: HandlerEntry, msg: Message) -> None:
        """Execute a handler and send dispatch_reply (new mode)."""
        try:
            resp = await entry.fn(msg)
        except Exception as exc:
            self._on_error(SDKError(
                kind=ErrorKind.HANDLER_EXCEPTION,
                message_id=msg.id,
                type=msg.type,
                from_did=msg.from_,
                cause=exc,
            ))
            try:
                await self._send_problem_report(msg, exc)
            except Exception:
                pass
            await self._send_dispatch_reply(
                msg.id, "error",
                code=type(exc).__name__,
                message=str(exc),
            )
            return

        if isinstance(resp, _Pass):
            await self._send_dispatch_reply(msg.id, "pass")
            return

        if isinstance(resp, Message):
            self._fill_response(resp, msg)
            try:
                await self._send_message(resp)
            except Exception as exc:
                self._on_error(SDKError(
                    kind=ErrorKind.TRANSPORT_WRITE,
                    message_id=msg.id,
                    type=msg.type,
                    from_did=msg.from_,
                    cause=exc,
                ))

        await self._send_dispatch_reply(msg.id, "handled")

    def _fill_response(self, resp: Message, original: Message) -> None:
        """Auto-fill response fields from the original message."""
        if not resp.from_:
            resp.from_ = self._agent_did
        if not resp.to and original.from_:
            resp.to = [original.from_]
        if not resp.thread_id:
            resp.thread_id = original.thread_id or original.id
        if not resp.id:
            resp.id = generate_id()

    async def _send_dispatch_reply(
        self,
        message_id: str,
        status: str,
        *,
        code: str = "",
        message: str = "",
    ) -> None:
        """Send a dispatch_reply event to the cloud node."""
        if not self._channel:
            return
        payload: dict[str, Any] = {
            "message_id": message_id,
            "status": status,
        }
        if code:
            payload["code"] = code
        if message:
            payload["message"] = message
        try:
            await self._channel.send_fire_and_forget("dispatch_reply", payload)
        except Exception:
            pass

    async def _send_problem_report(self, original: Message, err: Exception) -> None:
        """Send a DIDComm problem report for a handler error."""
        thread_id = original.thread_id or original.id
        report = Message(
            id=generate_id(),
            type="https://didcomm.org/report-problem/2.0/problem-report",
            from_=self._agent_did,
            to=[original.from_] if original.from_ else [],
            thread_id=thread_id,
            body={"code": "e.p.xfer.cant-process", "comment": str(err)},
        )
        await self._send_message(report)

    def _fill_message(self, msg: Message) -> None:
        """Auto-fill message ID and from fields."""
        if not msg.id:
            msg.id = generate_id()
        if not msg.from_:
            msg.from_ = self._agent_did

    async def _marshal_ordered(self, msg: Message) -> dict[str, Any]:
        """Attach grants and marshal, in call order. See ``_write_lock``."""
        async with self._write_lock:
            return marshal_didcomm(await self._with_grants(msg))

    async def _send_message(self, msg: Message) -> None:
        """Serialize and send a DIDComm message via the channel (fire-and-forget)."""
        if not self._channel:
            raise NotConnectedError()
        data = await self._marshal_ordered(msg)
        await self._channel.send_fire_and_forget("message", data)

    async def _send_message_acked(self, msg: Message) -> None:
        """Send a message and wait for server ack."""
        if not self._channel:
            raise NotConnectedError()
        data = await self._marshal_ordered(msg)
        reply = await self._channel.send("message", data)
        if reply.status == "error":
            raise ServerRejectError(reply.reason or reply.status)

    async def _send_message_fire_and_forget(self, msg: Message) -> None:
        """Send a message without waiting for server ack."""
        if not self._channel:
            raise NotConnectedError()
        data = await self._marshal_ordered(msg)
        await self._channel.send_fire_and_forget("message", data)

    # ------------------------------------------------------------------
    # Verifiable Grant attachment
    # ------------------------------------------------------------------

    async def _with_grants(self, msg: Message) -> Message:
        """Attach the Verifiable Grants that cover this message.

        The node requires one for anything its policy does not allow outright,
        and nothing in this SDK attached any — there was no enforcement on
        outgoing requests because there was no mechanism. An agent connecting
        directly sent nothing and was denied with "no grant covers this call": a
        message that reads as "your grant is misconfigured" when the truth is
        "no credential was ever put on the wire".

        Caller-supplied attachments WIN and are never displaced — someone
        passing their own has a reason, and silently overriding it would be the
        second confusing thing to happen to that message.

        With ONE narrowing, for identity credentials (see ``identity``). A
        message whose caller-supplied attachments are ALL identity credentials
        still gets the wallet's grants, appended after them. The two answer
        different questions — "who is the sender" and "what may it do" — and the
        node routes them to different policy inputs, so a caller who states who
        it is must not thereby stop stating what it may do. Under the old rule
        it did, and the denial that followed said "no grant covers this call":
        the exact misleading message this whole path exists to stop producing.
        Anything else the caller attaches — a grant, a document, a JSON blob —
        still displaces the wallet, unchanged.

        A wallet failure does NOT block the send. The node is the authority on
        whether this message needed a grant, and most traffic (discovery,
        trust-ping, problem reports) needs none; refusing here on a transient
        fetch error would take down calls that were never going to need us. The
        send proceeds unattached and ``on_grant_miss`` says so.

        "Does not block" has to hold for a read that HANGS, not just one that
        fails fast — a hang is the commoner production failure. The bound is
        ``Config.grant_read_timeout_ms``, enforced on the request itself, and a
        lapsed deadline arrives here as an ordinary read error.
        """
        if self._wallet is None:
            return msg

        own = msg.attachments or []
        if own and not all(is_identity_attachment(a) for a in own):
            return msg

        try:
            attachments = await self._wallet.attachments_for(
                msg.from_,
                recipients=msg.to or [],
                type_uri=msg.type,
                body=msg.body if msg.body is not None else msg._body_raw,
                # The cap left credentials off. Announced at once rather than
                # remembered for a denial: unlike "nothing covered it", this is
                # never the normal shape of a message that needs no grant, and
                # the holding that triggers it will trigger it on every send
                # until someone prunes the wallet.
                on_capped=lambda capped: self._notify_grant_miss(
                    GrantMissInfo(to=msg.to or [], type=msg.type, capped=capped)
                ),
            )
        except Exception as exc:
            # A read failure IS announced immediately: unlike "nothing covered
            # it", it is never normal, and it means every subsequent send is
            # flying blind.
            self._notify_grant_miss(
                GrantMissInfo(to=msg.to or [], type=msg.type, error=exc)
            )
            return msg

        if attachments:
            # The caller's entries stay, first and unmodified. The wallet only
            # ever appends here — `own` is empty in every case but the identity
            # one.
            msg.attachments = [*own, *attachments]
            return msg

        # Nothing covered it — remembered, not announced.
        #
        # Announcing here fired on every message that legitimately needs no
        # grant: discovery, trust-ping, problem reports. For the majority of
        # agents, which hold no grants at all, that is one callback per outbound
        # message — and a diagnostic that fires constantly is one nobody reads
        # when it matters. The signal actually wanted is "the node denied, and
        # we had attached nothing", which needs the denial: see _note_denial.
        self._remember_unattached(msg)
        return msg

    def _notify_grant_miss(self, info: GrantMissInfo) -> None:
        if self._on_grant_miss is None:
            return
        try:
            self._on_grant_miss(info)
        except Exception as exc:
            self._on_error(SDKError(kind=ErrorKind.HANDLER_EXCEPTION, cause=exc))

    def _remember_unattached(self, msg: Message, now_ms: float | None = None) -> None:
        """Record a message that went out unattached. Evicted by AGE, not count.

        A count cap drops the entry that mattered. The denial for a message
        arrives within seconds of it, but a cap counts every unattached message
        in between — and this records EVERY message it attaches nothing to,
        which for the agents this feature is aimed at (the ones holding no
        grants at all) is every discovery, trust-ping and problem report they
        send. Enough of those between the send and its denial and
        ``on_grant_miss`` never fires: the one thing it exists for, lost to
        traffic that never needed a grant.

        Age bounds the map by SEND RATE × window instead, which is the honest
        bound — the entries are small and the window is short.
        """
        now = now_ms if now_ms is not None else time.monotonic() * 1000

        # Insertion order is chronological and `now` never goes backwards, so
        # the stale entries are a prefix: stop at the first live one.
        for key in list(self._unattached):
            if now - self._unattached[key][0] < _UNATTACHED_WINDOW_MS:
                break
            del self._unattached[key]

        key = msg.thread_id or msg.id
        # Delete first: re-setting an existing key keeps its ORIGINAL position,
        # and one hot thread id refreshed in place would sit at the front with a
        # fresh timestamp and stop the prefix scan above from ever reaching the
        # stale entries behind it.
        self._unattached.pop(key, None)
        self._unattached[key] = (now, msg.to or [], msg.type)

    def _note_denial(self, msg: Message) -> None:
        """A problem report came back.

        If it is an authorization denial for a message we sent with nothing
        attached, that is the one case ``on_grant_miss`` exists for: the node
        names the grant it could not find, and only this side knows no
        credential was ever on the wire.
        """
        if msg.type != _PROBLEM_REPORT_TYPE:
            return

        body = msg.unmarshal_body()
        code = body.get("code", "") if isinstance(body, dict) else ""
        if not isinstance(code, str) or "authz" not in code:
            return

        # `parent_thread_id` is the one that matches in production and it is
        # SECOND only because a peer is free to use either. The node's own
        # denial sets `pthid` — to the denied message's `thid` or, for a message
        # sent without one, its `id` — and sets no `thid` at all.
        for key in (msg.thread_id, msg.parent_thread_id):
            hit = self._unattached.get(key) if key else None
            if hit is not None:
                del self._unattached[key]
                self._notify_grant_miss(
                    GrantMissInfo(to=hit[1], type=hit[2], denial_code=code)
                )
                return

    def _on_disconnect(self, err: Exception) -> None:
        """Internal disconnect handler that forwards to user callback."""
        if self._disconnect_fn:
            self._disconnect_fn(err)

    # ------------------------------------------------------------------
    # W3C Verifiable Credential APIs (REST, no WebSocket needed)
    # ------------------------------------------------------------------

    async def sign_credential(
        self,
        credential: Credential,
        *,
        issuer_did: str = "",
        format: str = "compact_jwt",
    ) -> str:
        """Sign a W3C Verifiable Credential.

        Uses the issuer DID's assertion key from the local wallet.

        Args:
            credential: The credential to sign.
            issuer_did: Override the issuer DID (defaults to ``self.did``).
            format: Output encoding (``compact_jwt``, ``json``, ``jwt``, ``enveloped``).

        Returns:
            The signed credential string.
        """
        body: dict[str, Any] = {
            "credential": credential.to_dict(),
            "issuer_did": issuer_did or self._agent_did,
            "format": format,
        }
        result = await self._rest.post("/api/v1/credentials/sign", body)
        return result["signed_credential"]  # type: ignore[no-any-return]

    async def verify_credential(
        self,
        signed_credential: str,
        *,
        verifier_did: str = "",
    ) -> VerifiedCredential:
        """Verify a signed credential.

        Args:
            signed_credential: The signed credential string.
            verifier_did: Override the verifier DID (defaults to ``self.did``).

        Returns:
            A :class:`VerifiedCredential` with the decoded credential and headers.
        """
        body: dict[str, Any] = {
            "signed_credential": signed_credential,
            "verifier_did": verifier_did or self._agent_did,
        }
        result = await self._rest.post("/api/v1/credentials/verify", body)
        return VerifiedCredential(
            credential=result.get("credential", {}),
            headers=result.get("headers", {}),
        )

    async def store_credential(
        self,
        credential_jwt: str,
        *,
        holder_did: str = "",
        issuer_did: str = "",
        valid_until: datetime | None = None,
    ) -> StoredCredential:
        """Store a signed credential JWT for a holder.

        Args:
            credential_jwt: The signed credential JWT.
            holder_did: Override the holder DID (defaults to ``self.did``).
            issuer_did: Optional issuer DID metadata.
            valid_until: Optional expiration timestamp.

        Returns:
            A :class:`StoredCredential` with the stored credential details.
        """
        body: dict[str, Any] = {
            "holder_did": holder_did or self._agent_did,
            "credential_jwt": credential_jwt,
        }
        if issuer_did:
            body["issuer_did"] = issuer_did
        if valid_until is not None:
            body["valid_until"] = valid_until.isoformat()

        result = await self._rest.post("/api/v1/credentials", body)
        return StoredCredential(
            id=result.get("id", ""),
            holder_did=result.get("holder_did", ""),
            credential_jwt=result.get("credential_jwt", ""),
            issuer_did=result.get("issuer_did", ""),
            valid_until=result.get("valid_until", ""),
        )

    async def list_credentials(
        self,
        *,
        holder_did: str = "",
    ) -> list[StoredCredential]:
        """List all stored credentials for a holder.

        Args:
            holder_did: Override the holder DID (defaults to ``self.did``).

        Returns:
            A list of :class:`StoredCredential` objects.
        """
        did = holder_did or self._agent_did
        path = "/api/v1/credentials?holder_did=" + url_quote(did, safe="")
        result = await self._rest.get(path)
        return [
            StoredCredential(
                id=c.get("id", ""),
                holder_did=c.get("holder_did", ""),
                credential_jwt=c.get("credential_jwt", ""),
                issuer_did=c.get("issuer_did", ""),
                valid_until=c.get("valid_until", ""),
            )
            for c in result.get("credentials", [])
        ]

    async def get_credential(self, credential_id: str) -> StoredCredential:
        """Retrieve a stored credential by ID.

        Args:
            credential_id: The credential ID to look up.

        Returns:
            A :class:`StoredCredential` with the stored credential details.
        """
        path = "/api/v1/credentials/" + url_quote(credential_id, safe="")
        result = await self._rest.get(path)
        return StoredCredential(
            id=result.get("id", ""),
            holder_did=result.get("holder_did", ""),
            credential_jwt=result.get("credential_jwt", ""),
            issuer_did=result.get("issuer_did", ""),
            valid_until=result.get("valid_until", ""),
        )

    # ------------------------------------------------------------------
    # W3C Verifiable Presentation APIs (REST, no WebSocket needed)
    # ------------------------------------------------------------------

    async def sign_presentation(
        self,
        credentials: list[str],
        *,
        holder_did: str = "",
        format: str = "compact_jwt",
        nonce: str = "",
    ) -> str:
        """Sign a W3C Verifiable Presentation wrapping signed credentials.

        Uses the holder's authentication key (not assertion key).

        Args:
            credentials: List of signed credential JWTs to include.
            holder_did: Override the holder DID (defaults to ``self.did``).
            format: Output encoding (``compact_jwt``, ``json``, ``jwt``, ``enveloped``).
            nonce: Optional nonce / challenge string.

        Returns:
            The signed presentation string.
        """
        body: dict[str, Any] = {
            "credentials": credentials,
            "holder_did": holder_did or self._agent_did,
            "format": format,
        }
        if nonce:
            body["nonce"] = nonce

        result = await self._rest.post("/api/v1/presentations/sign", body)
        return result["signed_presentation"]  # type: ignore[no-any-return]

    async def verify_presentation(
        self,
        signed_presentation: str,
        *,
        verifier_did: str = "",
    ) -> VerifiedPresentation:
        """Verify a signed presentation.

        Args:
            signed_presentation: The signed presentation string.
            verifier_did: Override the verifier DID (defaults to ``self.did``).

        Returns:
            A :class:`VerifiedPresentation` with the decoded presentation and headers.
        """
        body: dict[str, Any] = {
            "signed_presentation": signed_presentation,
            "verifier_did": verifier_did or self._agent_did,
        }
        result = await self._rest.post("/api/v1/presentations/verify", body)
        return VerifiedPresentation(
            presentation=result.get("presentation", {}),
            headers=result.get("headers", {}),
        )