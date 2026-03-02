"""Layr8 DIDComm Agent Client."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .channel import PhoenixChannel
from .config import Config, resolve_config
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
from .message import Message, generate_id, marshal_didcomm, parse_didcomm


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
        *,
        manual_ack: bool = False,
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
            self._registry.register(msg_type, fn, manual_ack=manual_ack)
            return None

        # Decorator mode
        def decorator(handler: HandlerFn) -> HandlerFn:
            self._registry.register(msg_type, handler, manual_ack=manual_ack)
            return handler

        return decorator

    async def connect(self) -> None:
        """Establish WebSocket connection and join the Phoenix Channel."""
        if self._connected:
            raise AlreadyConnectedError()
        if self._closed:
            raise ClientClosedError()

        protocols = self._registry.protocols()

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

        # Check if this is a response to a pending Request (by thread ID)
        if msg.thread_id and msg.thread_id in self._pending:
            future = self._pending.pop(msg.thread_id)
            if not future.done():
                future.set_result(msg)
            return

        # Route to registered handler
        entry = self._registry.lookup(msg.type)
        if not entry:
            self._on_error(SDKError(
                kind=ErrorKind.NO_HANDLER,
                message_id=msg.id,
                type=msg.type,
                from_did=msg.from_,
            ))
            return

        # Auto-ack before handler (unless manual ack)
        if not entry.manual_ack:
            task = asyncio.ensure_future(self._channel.send_ack([msg.id]))  # type: ignore[union-attr]
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        else:
            def _manual_ack(mid: str) -> asyncio.Task[None]:
                t = asyncio.ensure_future(
                    self._channel.send_ack([mid])  # type: ignore[union-attr]
                )
                t.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                return t
            msg._ack_fn = _manual_ack

        # Run handler asynchronously
        asyncio.ensure_future(self._run_handler(entry, msg))

    async def _run_handler(self, entry: HandlerEntry, msg: Message) -> None:
        """Execute a handler and send back the response or problem report."""
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

        if resp is not None:
            # Auto-fill response fields
            if not resp.from_:
                resp.from_ = self._agent_did
            if not resp.to and msg.from_:
                resp.to = [msg.from_]
            if not resp.thread_id:
                resp.thread_id = msg.thread_id or msg.id
            if not resp.id:
                resp.id = generate_id()
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

    async def _send_message(self, msg: Message) -> None:
        """Serialize and send a DIDComm message via the channel (fire-and-forget)."""
        if not self._channel:
            raise NotConnectedError()
        data = marshal_didcomm(msg)
        await self._channel.send_fire_and_forget("message", data)

    async def _send_message_acked(self, msg: Message) -> None:
        """Send a message and wait for server ack."""
        if not self._channel:
            raise NotConnectedError()
        data = marshal_didcomm(msg)
        reply = await self._channel.send("message", data)
        if reply.status == "error":
            raise ServerRejectError(reply.reason or reply.status)

    async def _send_message_fire_and_forget(self, msg: Message) -> None:
        """Send a message without waiting for server ack."""
        if not self._channel:
            raise NotConnectedError()
        data = marshal_didcomm(msg)
        await self._channel.send_fire_and_forget("message", data)

    def _on_disconnect(self, err: Exception) -> None:
        """Internal disconnect handler that forwards to user callback."""
        if self._disconnect_fn:
            self._disconnect_fn(err)
