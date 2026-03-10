"""Phoenix Channel V2 transport over WebSocket."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import websockets
import websockets.asyncio.client

from .backoff import Backoff


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
    ) -> None:
        self._ws_url = ws_url
        self._api_key = api_key
        self._topic = f"plugins:{agent_did}"
        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._on_reconnect = on_reconnect

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
            self._ws = await websockets.asyncio.client.connect(
                full_url,
                sock=sock,
                open_timeout=10,
            )
        except Exception as exc:
            if sock:
                sock.close()
            raise _make_connection_error(self._ws_url, exc) from exc

        self._read_task = asyncio.create_task(self._read_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        await self._join(self._protocols)

    async def _join(self, protocols: list[str]) -> None:
        """Send phx_join and wait for the reply."""
        ref = self._next_ref()
        self._join_ref = ref

        join_payload: dict[str, Any] = {
            "payload_types": protocols,
            "did_spec": {
                "mode": "Create",
                "storage": "ephemeral",
                "type": "plugin",
                "verificationMethods": [
                    {"purpose": "authentication"},
                    {"purpose": "assertionMethod"},
                    {"purpose": "keyAgreement"},
                ],
            },
        }

        loop = asyncio.get_running_loop()
        self._join_future = loop.create_future()

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
        elif event in ("phx_error", "phx_close"):
            if self._on_disconnect:
                self._on_disconnect(Exception(f"channel {event}"))

    async def _heartbeat_loop(self) -> None:
        """Send heartbeat every 30 seconds."""
        try:
            while not self._closed:
                await asyncio.sleep(30)
                if self._closed or not self._ws:
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
