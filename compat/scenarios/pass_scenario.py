"""Pass scenario — handler returns PASS sentinel.

Tests that when a handler returns PASS, the cloud-node's built-in
trust-ping handler takes over and sends a ping-response.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from layr8 import Client, Config, Message, PASS, log_errors

from .types import ScenarioContext, SenderContext, ScenarioResult, elapsed_ms

PING_TYPE = "https://didcomm.org/trust-ping/2.0/ping"
PING_RESPONSE_TYPE = "https://didcomm.org/trust-ping/2.0/ping-response"
TRUST_PING_PROTOCOL = "https://didcomm.org/trust-ping/2.0"


async def run_receiver(
    ctx: ScenarioContext,
    on_ready: Callable[[str], None] | None = None,
) -> None:
    """Connect and register a handler that returns PASS. Blocks until cancelled."""
    client = Client(
        Config(node_url=ctx.node_url, api_key=ctx.api_key, agent_did=ctx.agent_did),
        log_errors(),
    )

    @client.handle(PING_TYPE)
    async def handler(msg: Message) -> Message | None:
        return PASS  # type: ignore[return-value]

    async with client:
        if on_ready:
            on_ready(client.did)
        await asyncio.Event().wait()


async def run_sender(ctx: SenderContext) -> ScenarioResult:
    """Send a trust-ping and verify the cloud-node responds with ping-response."""
    client = Client(
        Config(
            node_url=ctx.node_url,
            api_key=ctx.api_key,
            agent_did=ctx.agent_did,
            protocols=[TRUST_PING_PROTOCOL],
        ),
        log_errors(),
    )
    start = time.monotonic()

    try:
        async with client:
            try:
                await client.request(
                    Message(
                        type=PING_TYPE,
                        to=[ctx.receiver_did],
                        body={"responseRequested": True},
                    ),
                    timeout=ctx.timeout,
                )
                return ScenarioResult("pass", "pass", elapsed_ms(start))
            except asyncio.TimeoutError:
                return ScenarioResult(
                    "fail", "pass", elapsed_ms(start),
                    error="expected ping-response but got timeout",
                )
    except Exception as e:
        return ScenarioResult("fail", "pass", elapsed_ms(start), error=str(e))