"""Phoenix Channel V2 transport over WebSocket."""

from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import websockets
import websockets.asyncio.client

from .backoff import Backoff
from .delegated import (
    DELEGATION_REFRESH_CAPABILITY,
    DelegatedCredentialsReading,
    join_revision,
    parse_delegated_credentials,
    parse_delegation_push,
)


@dataclass
class ServerReply:
    """Parsed reply from the Phoenix server for a sent message."""

    status: str = ""
    reason: str = ""


def _is_localhost(host: str) -> bool:
    """Return True if host is 'localhost' or a subdomain of it (RFC 6761)."""
    return host == "localhost" or host.endswith(".localhost")


def _create_localhost_socket(ws_url: str) -> socket.socket | None:
    """
    Create a pre-connected TCP socket to 127.0.0.1 for *.localhost URLs (RFC 6761).

    The websockets library does not reliably override the Host header via
    additional_headers when connecting to 127.0.0.1. Instead, we pre-connect
    a raw socket to loopback and pass it to websockets.connect(), which then
    sends the correct Host header derived from the original URL.

    Returns a connected socket if the host is *.localhost, or None otherwise.
    """
    parsed = urlparse(ws_url)
    hostname = parsed.hostname or ""
    if not _is_localhost(hostname):
        return None
    port = parsed.port or (443 if parsed.scheme in ("wss", "https") else 80)
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    return sock


class PhoenixChannel:
    """
    Phoenix Channel transport over WebSocket.

    Implements the same wire protocol as the Go SDK's phoenixChannel:
    V2 JSON array format [join_ref, ref, topic, event, payload].
    """

    def __init__(
        self,
        ws_url: str,
        api_key: str,
        agent_did: str,
        *,
        on_message: Callable[[Any], None],
        on_disconnect: Callable[[Exception], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
        parent_did: str = "",
        child_name_source: str = "",
        on_delegated_credentials: Callable[
            [str, DelegatedCredentialsReading | None], None
        ] | None = None,
        on_delegation_refreshed: Callable[
            [str, DelegatedCredentialsReading], None
        ] | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._api_key = api_key
        self._agent_did = agent_did
        self._topic = f"plugins:{agent_did}"
        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._on_reconnect = on_reconnect
        # Both are sent inside did_spec only when set, so a join that names no
        # parent puts exactly the payload on the wire it put there before these
        # existed. See layr8.child_did.
        self._parent_did = parent_did
        self._child_name_source = child_name_source
        self._on_delegated_credentials = on_delegated_credentials
        # None until a join reply that names a parent arrives. None is its own
        # reading — "no reading" — and is never coerced into an empty complete
        # one. See DelegatedCredentialsReading.
        self._delegated: DelegatedCredentialsReading | None = None
        self._ephemeral_delegation: bool = False
        self._on_delegation_refreshed = on_delegation_refreshed
        # Whether the node announced ephemeral_delegation_refresh/1, and
        # whether the last join asked for it. A push is applied only then.
        self._delegation_refresh: bool = False
        self._refresh_requested: bool = False
        # Revision of the reading in _delegated; reset by every join.
        self._delegation_revision: int | None = None
        # Pushes that arrived while a join was waiting for its reply, in arrival
        # order; None when no join is in flight.
        #
        # The node can push right behind its join reply. _handle_inbound only
        # completes _join_future, and _join resumes on a later loop turn, so
        # the read loop can dispatch that push first: it would be compared with
        # the previous join's state (or with none) and could then be
        # overwritten by the older join reading. It is held here instead and
        # applied once _join has installed its reading.
        self._held_pushes: list[Any] | None = None

        self._ws: websockets.asyncio.client.ClientConnection | None = None
        self._ref_counter = 0
        self._join_ref = ""
        self._assigned_did = ""
        self._closed = False
        self._reconnecting: bool = False
        self._protocols: list[str] = []
        self._read_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._join_future: asyncio.Future[Any] | None = None
        self._pending_refs: dict[str, asyncio.Future[ServerReply]] = {}
        # Monotonic timestamp (time.monotonic()) of the most recently
        # observed inbound Phoenix frame. Powers the application-layer
        # watchdog in _heartbeat_loop — closes #5 by detecting "TCP healthy
        # but Phoenix Channel GenServer hung" within ~75s.
        #
        # WS-level liveness (TCP / NAT / LB half-dead) is covered separately
        # by the `websockets` library's built-in ping/pong mechanism, made
        # explicit in the connect() call below — closes #4.
        self._last_frame_at = time.monotonic()
        self._reply_protocol: bool = False

    async def connect(self, protocols: list[str]) -> None:
        """Establish WebSocket connection and join the Phoenix channel."""
        self._protocols = protocols
        await self._dial()

    async def _dial(self) -> None:
        """Open the WebSocket and join the channel (used by connect and reconnect)."""
        self._ref_counter = 0

        parsed = urlparse(self._ws_url)
        qs = parse_qs(parsed.query)
        qs["api_key"] = [self._api_key]
        qs["vsn"] = ["2.0.0"]
        new_query = urlencode(qs, doseq=True)
        full_url = urlunparse(parsed._replace(query=new_query))

        # For *.localhost, pre-connect a raw socket to 127.0.0.1 so the
        # websockets library sends the correct Host header from the URL.
        sock = _create_localhost_socket(full_url)

        try:
            # Explicit WS-level ping/pong (issue #4 — defense-in-depth):
            # `websockets` defaults to ping_interval=20, ping_timeout=20
            # which already protects against TCP / NAT / LB half-dead. We
            # set them explicitly so a future maintainer can't accidentally
            # disable the layer (e.g. by passing ping_interval=None in a
            # test scenario and shipping it). 30 / 20 leaves a 50s detection
            # window — well under AWS NLB's 350s idle timeout — and matches
            # the policy enforced in go-sdk.
            self._ws = await websockets.asyncio.client.connect(
                full_url,
                sock=sock,
                open_timeout=10,
                ping_interval=30,
                ping_timeout=20,
                close_timeout=10,
            )
        except Exception as exc:
            if sock:
                sock.close()
            raise _make_connection_error(self._ws_url, exc) from exc

        # Reset the watchdog clock so the first heartbeat tick after
        # (re)connect measures silence from "just now", not from before
        # the disconnect.
        self._last_frame_at = time.monotonic()
        self._read_task = asyncio.create_task(self._read_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        await self._join(self._protocols)

    async def _join(self, protocols: list[str]) -> None:
        """Send phx_join and wait for the reply."""
        ref = self._next_ref()
        self._join_ref = ref

        # A fixed identity (agent_did given) joins as a PERSISTENT twin. Since
        # cloud-node 4.19.3x (2026-09-08) a twin joined with
        # storage "ephemeral" is reclaimed the moment it disconnects, and
        # everything stored on the twin — its mediator declaration above all —
        # dies with it, so messages sent while the agent was offline were
        # dropped at the node instead of queued. Only a node-assigned
        # per-session DID (blank agent_did) is ephemeral.
        #
        # EXCEPT when this DID borrows a parent's authority. Only a temporary
        # identity may borrow: the node refuses a join that names a parent and
        # declares "persistent" with
        # `e.join.plugin.child.storage-not-ephemeral`. A borrowed DID is a
        # fixed identity by construction — it is `<parent>:<segment>`, settled
        # in resolve_config so a reconnect returns under the same name — so the
        # rule above would send "persistent" for EVERY borrowed join and the
        # node would refuse every one of them. A borrowed twin does not need
        # persistence for the reason the rule exists either: its authority is
        # re-minted on each join, and the parent, which the node requires to be
        # persistent, is what survives.
        if self._parent_did:
            storage = "ephemeral"
        else:
            storage = "persistent" if self._agent_did else "ephemeral"

        did_spec: dict[str, Any] = {
            "mode": "Create",
            "storage": storage,
            "type": "plugin",
            "verificationMethods": [
                {"purpose": "authentication"},
                {"purpose": "assertionMethod"},
                {"purpose": "keyAgreement"},
            ],
        }
        # Sent only when set, so a join that names no parent is byte for byte
        # the payload this SDK sent before the field existed.
        if self._parent_did:
            did_spec["parentDid"] = self._parent_did
        # Sent only when a parent was named and somebody therefore chose a
        # borrower's name. An empty value is not sent at all, so "this client
        # does not report it" stays a third answer rather than becoming "the
        # caller chose it".
        if self._child_name_source:
            did_spec["childNameSource"] = self._child_name_source

        join_payload: dict[str, Any] = {
            "payload_types": protocols,
            "reply_protocol": True,
            "did_spec": did_spec,
        }
        # Ask the node to keep a borrowed child's set current. Only a join that
        # names a parent borrows anything, so the key is not sent otherwise and
        # an unparented join is byte for byte what it was before.
        request_refresh = bool(self._parent_did)
        if request_refresh:
            join_payload["delegation_refresh"] = True

        loop = asyncio.get_running_loop()
        self._join_future = loop.create_future()
        # Hold pushes from here until this join has installed its reading.
        self._held_pushes = []
        try:
            await self._join_and_install(ref, join_payload, request_refresh)
        except BaseException:
            self._held_pushes = None
            raise
        held, self._held_pushes = self._held_pushes or [], None
        for payload in held:
            self._apply_delegation_push(payload)

    async def _join_and_install(
        self, ref: str, join_payload: dict[str, Any], request_refresh: bool
    ) -> None:
        assert self._join_future is not None
        await self._write_msg(ref, ref, self._topic, "phx_join", join_payload)

        try:
            reply = await asyncio.wait_for(self._join_future, timeout=10)
        except asyncio.TimeoutError:
            raise _make_connection_error(self._ws_url, "join timed out")

        status = reply.get("status")
        if status != "ok":
            response = reply.get("response", {})
            reason = response.get("reason", "") if isinstance(response, dict) else ""
            if reason:
                raise _make_connection_error(self._ws_url, reason)
            raise _make_connection_error(
                self._ws_url, f"join rejected: {status}"
            )

        response = reply.get("response", {})
        if isinstance(response, dict) and response.get("did"):
            self._assigned_did = response["did"]
        capabilities = response.get("capabilities", []) if isinstance(response, dict) else []
        self._reply_protocol = "reply_protocol/1" in capabilities
        self._ephemeral_delegation = "ephemeral_delegation/1" in capabilities
        self._delegation_refresh = DELEGATION_REFRESH_CAPABILITY in capabilities
        self._refresh_requested = request_refresh

        # The node omits the key when the join named no parent, and otherwise
        # sends a reading that says whether it could read the parent's wallet at
        # all. Only a well-formed reading is a reading; `or []` here would be
        # the bug this whole object exists to avoid.
        raw_delegated = response.get("delegated_credentials") if isinstance(response, dict) else None
        self._delegated = parse_delegated_credentials(raw_delegated)
        # A rejoin starts the revision again from the join reply, so a push on
        # the new connection is never compared with one from the old.
        self._delegation_revision = (
            None if self._delegated is None else join_revision(raw_delegated)
        )
        # UNCONDITIONAL, None included. A rejoin whose reply carries no reading
        # is a rejoin after which the previous set must go: it was minted for a
        # DID document this rejoin may have replaced, and the node that would
        # have re-minted it did not. Firing only when there is something to hand
        # over leaves the wallet attaching the last join's credentials while
        # delegated_credentials reports there are none.
        if self._on_delegated_credentials is not None:
            self._on_delegated_credentials(
                self._agent_did or self._assigned_did, self._delegated
            )

    async def send(self, event: str, payload: Any) -> ServerReply:
        """Send a Phoenix Channel event and wait for server reply."""
        ref = self._next_ref()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ServerReply] = loop.create_future()
        self._pending_refs[ref] = future

        try:
            await self._write_msg(None, ref, self._topic, event, payload)
            return await asyncio.wait_for(future, timeout=15)
        except asyncio.TimeoutError:
            self._pending_refs.pop(ref, None)
            raise
        except Exception:
            self._pending_refs.pop(ref, None)
            raise

    async def send_fire_and_forget(self, event: str, payload: Any) -> None:
        """Send a Phoenix Channel event without waiting for server reply."""
        ref = self._next_ref()
        await self._write_msg(None, ref, self._topic, event, payload)

    async def send_ack(self, ids: list[str]) -> None:
        """Acknowledge message IDs to the cloud-node."""
        await self.send_fire_and_forget("ack", {"ids": ids})

    @property
    def assigned_did(self) -> str:
        return self._assigned_did

    @property
    def reply_protocol(self) -> bool:
        return self._reply_protocol

    @property
    def delegated_credentials(self) -> DelegatedCredentialsReading | None:
        """What the last join learned about the parent's wallet, or ``None``."""
        return self._delegated

    @property
    def supports_ephemeral_delegation(self) -> bool:
        """Whether the node advertised ``ephemeral_delegation/1`` at join."""
        return self._ephemeral_delegation

    @property
    def supports_ephemeral_delegation_refresh(self) -> bool:
        """Whether the node advertised ``ephemeral_delegation_refresh/1`` at join."""
        return self._delegation_refresh

    @property
    def delegation_revision(self) -> int | None:
        """Revision of :attr:`delegated_credentials`; ``None`` with no reading."""
        return self._delegation_revision

    def _apply_delegation_push(self, payload: Any) -> None:
        """Apply an inbound ``delegated_credentials`` push.

        The push carries the WHOLE current set, so applying it replaces what is
        held; it never appends. It is dropped, and the last reading stands,
        when this join did not ask for refreshes, there is no reading to
        replace (the join named no parent), it does not parse, or its revision
        is not greater than the one held.

        The wallet swap in ``on_delegated_credentials`` is one assignment of a
        new list, on the event loop. A send already choosing its attachments
        took the old list first, so it uses the old set or the new one, never
        a mix.
        """
        if not self._refresh_requested or self._closed:
            return
        if self._delegated is None or self._delegation_revision is None:
            return
        push = parse_delegation_push(payload)
        if push is None:
            return
        reading, revision = push
        if revision <= self._delegation_revision:
            return
        self._delegated = reading
        self._delegation_revision = revision
        did = self._agent_did or self._assigned_did
        if self._on_delegated_credentials is not None:
            self._on_delegated_credentials(did, reading)
        if self._on_delegation_refreshed is not None:
            self._on_delegation_refreshed(did, reading)

    async def close(self) -> None:
        """Send phx_leave and shut down gracefully."""
        if self._closed:
            return
        self._closed = True
        self._reconnecting = False

        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # Cancel all pending ref futures
        for ref, future in list(self._pending_refs.items()):
            if not future.done():
                future.cancel()
        self._pending_refs.clear()

        if self._ws:
            try:
                ref = self._next_ref()
                await self._write_msg(None, ref, self._topic, "phx_leave", {})
            except Exception:
                pass
            await self._ws.close()
            self._ws = None

        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

    async def _read_loop(self) -> None:
        """Continuously read WebSocket messages and dispatch them."""
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if self._closed:
                    return
                # Any inbound frame proves the connection is alive
                # end-to-end — update the watchdog clock before parsing.
                # Heartbeat ack, message event, phx_reply, phx_error,
                # all count.
                self._last_frame_at = time.monotonic()
                try:
                    arr = json.loads(raw)
                    if not isinstance(arr, list) or len(arr) != 5:
                        continue
                    join_ref, ref, topic, event, payload = arr
                    self._handle_inbound(join_ref, ref, topic, event, payload)
                except (json.JSONDecodeError, ValueError):
                    continue
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as exc:
            if not self._closed:
                if self._on_disconnect:
                    self._on_disconnect(exc)
                self._reject_pending_refs()
                if not self._reconnecting:
                    self._reconnect_task = asyncio.create_task(self._reconnect_loop())
                return

        # Iterator ended (clean close) or ConnectionClosed — trigger reconnect
        if not self._closed:
            if self._on_disconnect:
                self._on_disconnect(Exception("WebSocket closed"))
            self._reject_pending_refs()
            if not self._reconnecting:
                self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    def _handle_inbound(
        self,
        join_ref: str | None,
        ref: str | None,
        topic: str,
        event: str,
        payload: Any,
    ) -> None:
        """Route inbound Phoenix messages to the appropriate handler."""
        if event == "phx_reply":
            # Join reply
            if self._join_future and not self._join_future.done() and ref == self._join_ref:
                self._join_future.set_result(payload)
                return

            # Message send reply (ref tracking)
            if ref and ref in self._pending_refs:
                future = self._pending_refs.pop(ref)
                if not future.done():
                    status = ""
                    reason = ""
                    if isinstance(payload, dict):
                        status = payload.get("status", "")
                        response = payload.get("response", {})
                        if isinstance(response, dict):
                            reason = response.get("reason", "")
                    future.set_result(ServerReply(status=status, reason=reason))
        elif event == "message":
            self._on_message(payload)
        elif event == "delegated_credentials":
            # A replacement reading for a borrowed child. The channel decides
            # whether it applies; while a join is in flight it is held until
            # the join has installed its own reading (see _held_pushes).
            if self._held_pushes is not None:
                self._held_pushes.append(payload)
                return
            self._apply_delegation_push(payload)
        elif event in ("phx_error", "phx_close"):
            if self._on_disconnect:
                self._on_disconnect(Exception(f"channel {event}"))

    # Maximum time a connection may be silent before the Phoenix-layer
    # watchdog treats it as hung. 2.5× the heartbeat interval — tolerates
    # one missed reply, trips on two consecutive misses. Catches the case
    # where TCP + cowboy pong are healthy but cloud-node's per-tenant
    # Phoenix Channel GenServer has stopped processing (OOM cascade,
    # deploy window, etc.) — closes #5.
    _HEARTBEAT_INTERVAL_S = 30
    _HEARTBEAT_MAX_SILENT_S = 75

    async def _heartbeat_loop(self) -> None:
        """Send heartbeat every 30 seconds; close the WS if no inbound frame
        has arrived within _HEARTBEAT_MAX_SILENT_S.
        """
        try:
            while not self._closed:
                await asyncio.sleep(self._HEARTBEAT_INTERVAL_S)
                if self._closed or not self._ws:
                    return

                silent = time.monotonic() - self._last_frame_at
                if silent > self._HEARTBEAT_MAX_SILENT_S:
                    # Application-layer hang: TCP + cowboy pong are still
                    # working (we wouldn't be here otherwise), but the
                    # Phoenix Channel GenServer is no longer responding to
                    # heartbeats. Close the socket; the read loop's
                    # ConnectionClosed handler will schedule reconnect.
                    try:
                        await self._ws.close()
                    except Exception:
                        # ignore — already in a bad state
                        pass
                    return

                ref = self._next_ref()
                await self._write_msg(None, ref, "phoenix", "heartbeat", {})
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def _next_ref(self) -> str:
        self._ref_counter += 1
        return str(self._ref_counter)

    async def _write_msg(
        self,
        join_ref: str | None,
        ref: str | None,
        topic: str,
        event: str,
        payload: Any,
    ) -> None:
        if not self._ws or self._reconnecting:
            from .errors import NotConnectedError

            raise NotConnectedError()
        data = json.dumps([join_ref, ref, topic, event, payload])
        await self._ws.send(data)

    def _reject_pending_refs(self) -> None:
        """Cancel all pending ref futures."""
        for ref, future in list(self._pending_refs.items()):
            if not future.done():
                future.cancel()
        self._pending_refs.clear()

    async def _reconnect_loop(self) -> None:
        """Attempt to reconnect with exponential backoff."""
        if self._reconnecting:
            return
        self._reconnecting = True

        # Clean up old connection
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._heartbeat_task:
            self._heartbeat_task.cancel()

        bo = Backoff(1.0, 30.0)

        while not self._closed:
            delay = bo.next()
            await asyncio.sleep(delay)
            if self._closed:
                return
            try:
                self._reconnecting = False
                await self._dial()
                if self._on_reconnect:
                    self._on_reconnect()
                return
            except Exception:
                self._reconnecting = True
                continue


def _make_connection_error(
    url: str, exc: Exception | str
) -> Exception:
    from .errors import Layr8ConnectionError

    reason = str(exc) if isinstance(exc, Exception) else exc
    return Layr8ConnectionError(url=url, reason=reason)