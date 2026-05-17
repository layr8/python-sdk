"""Wildcard scenario — catch-all handler via handle_all.

Tests that a receiver using handle_all can respond to any message type.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from layr8 import Client, Config, Message, log_errors

from .types import ScenarioContext, SenderContext, ScenarioResult, elapsed_ms

WILDCARD_REQUEST_TYPE = "https://layr8.test/wildcard/1.0/request"
WILDCARD_RESPONSE_TYPE = "https://layr8.test/wildcard/1.0/response"


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
        return Message(
            type=WILDCARD_RESPONSE_TYPE,
            body={"received": body, "from": client.did},
        )

    async with client:
        if on_ready:
            on_ready(client.did)
        await asyncio.Event().wait()


async def run_sender(ctx: SenderContext) -> ScenarioResult:
    """Send a message with an arbitrary type and verify catch-all responds."""
    client = Client(
        Config(node_url=ctx.node_url, api_key=ctx.api_key, agent_did=ctx.agent_did),
        log_errors(),
    )
    start = time.monotonic()

    try:
        async with client:
            resp = await client.request(
                Message(
                    type=WILDCARD_REQUEST_TYPE,
                    to=[ctx.receiver_did],
                    body={"ping": ctx.test_id},
                ),
                timeout=ctx.timeout,
            )
            body = resp.unmarshal_body()
            received = body.get("received", {})
            if isinstance(received, dict) and received.get("ping") == ctx.test_id:
                return ScenarioResult("pass", "wildcard", elapsed_ms(start))
            return ScenarioResult(
                "fail", "wildcard", elapsed_ms(start),
                error=f"unexpected response: {received!r}",
            )
    except Exception as e:
        return ScenarioResult("fail", "wildcard", elapsed_ms(start), error=str(e))