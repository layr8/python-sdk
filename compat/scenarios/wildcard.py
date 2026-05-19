"""Wildcard scenario — catch-all handler via handle_all.

Tests that a receiver using handle_all can respond to any message type,
including both custom protocols and standard protocols like trust-ping.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from layr8 import Client, Config, Message, log_errors

from .types import ScenarioContext, SenderContext, ScenarioResult, elapsed_ms

ECHO_TYPE = "https://layr8.test/echo/1.0/request"
ECHO_RESPONSE_TYPE = "https://layr8.test/echo/1.0/response"
PING_TYPE = "https://didcomm.org/trust-ping/2.0/ping"
PING_RESPONSE_TYPE = "https://didcomm.org/trust-ping/2.0/ping-response"
WILDCARD_RESPONSE_TYPE = "https://layr8.test/wildcard/1.0/response"

ECHO_PROTOCOL = "https://layr8.test/echo/1.0"
TRUST_PING_PROTOCOL = "https://didcomm.org/trust-ping/2.0"


async def run_receiver(
    ctx: ScenarioContext,
    on_ready: Callable[[str], None] | None = None,
) -> None:
    """Connect with only a catch-all handler. Blocks until cancelled."""
    client = Client(
        Config(node_url=ctx.node_url, api_key=ctx.api_key, agent_did=ctx.agent_did),
        log_errors(),
    )

    @client.handle_all
    async def catch_all(msg: Message) -> Message:
        body = msg.unmarshal_body()
        reply = {"received": body, "from": client.did}
        if msg.type == PING_TYPE:
            reply["intercepted"] = True

        if msg.type == ECHO_TYPE:
            reply_type = ECHO_RESPONSE_TYPE
        elif msg.type == PING_TYPE:
            reply_type = PING_RESPONSE_TYPE
        else:
            reply_type = WILDCARD_RESPONSE_TYPE

        return Message(
            type=reply_type,
            body=reply,
        )

    async with client:
        if on_ready:
            on_ready(client.did)
        await asyncio.Event().wait()


async def run_sender(ctx: SenderContext) -> ScenarioResult:
    """Send two messages and verify catch-all responds to both."""
    client = Client(
        Config(
            node_url=ctx.node_url,
            api_key=ctx.api_key,
            agent_did=ctx.agent_did,
            protocols=[ECHO_PROTOCOL, TRUST_PING_PROTOCOL],
        ),
        log_errors(),
    )
    try:
        async with client:
            start = time.monotonic()
            # 1. Send echo request — proves catch-all handles custom protocols.
            resp = await client.request(
                Message(
                    type=ECHO_TYPE,
                    to=[ctx.receiver_did],
                    body={"ping": ctx.test_id},
                ),
                timeout=ctx.timeout,
            )
            body = resp.unmarshal_body()
            received = body.get("received", {})
            if not (isinstance(received, dict) and received.get("ping") == ctx.test_id):
                return ScenarioResult(
                    "fail", "wildcard", elapsed_ms(start),
                    error=f"unexpected echo response: {received!r}",
                )

            # 2. Send trust-ping — proves catch-all intercepts standard protocols.
            resp = await client.request(
                Message(
                    type=PING_TYPE,
                    to=[ctx.receiver_did],
                    body={"responseRequested": True},
                ),
                timeout=ctx.timeout,
            )
            body = resp.unmarshal_body()
            if not body.get("intercepted"):
                return ScenarioResult(
                    "fail", "wildcard", elapsed_ms(start),
                    error=f"trust-ping not intercepted by plugin: {body!r}",
                )

            return ScenarioResult("pass", "wildcard", elapsed_ms(start))
    except Exception as e:
        return ScenarioResult("fail", "wildcard", elapsed_ms(start), error=str(e))