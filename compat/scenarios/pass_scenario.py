"""Pass scenario — handler returns PASS sentinel.

Tests that when a handler returns PASS, the cloud-node treats
the message as unhandled (no response is sent back to the sender).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from layr8 import Client, Config, Message, PASS, log_errors

from .types import ScenarioContext, SenderContext, ScenarioResult, elapsed_ms

PASS_TYPE = "https://layr8.test/pass/1.0/request"


async def run_receiver(
    ctx: ScenarioContext,
    on_ready: Callable[[str], None] | None = None,
) -> None:
    """Connect and register a handler that returns PASS. Blocks until cancelled."""
    client = Client(
        Config(node_url=ctx.node_url, api_key=ctx.api_key, agent_did=ctx.agent_did),
        log_errors(),
    )

    @client.handle(PASS_TYPE)
    async def handler(msg: Message) -> Message | None:
        return PASS  # type: ignore[return-value]

    async with client:
        if on_ready:
            on_ready(client.did)
        await asyncio.Event().wait()


async def run_sender(ctx: SenderContext) -> ScenarioResult:
    """Send a message and verify no response comes back (timeout expected)."""
    client = Client(
        Config(node_url=ctx.node_url, api_key=ctx.api_key, agent_did=ctx.agent_did),
        log_errors(),
    )
    start = time.monotonic()

    try:
        async with client:
            try:
                await client.request(
                    Message(
                        type=PASS_TYPE,
                        to=[ctx.receiver_did],
                        body={"test_id": ctx.test_id},
                    ),
                    timeout=ctx.timeout,
                )
                return ScenarioResult(
                    "fail", "pass", elapsed_ms(start),
                    error="expected timeout but got response",
                )
            except asyncio.TimeoutError:
                return ScenarioResult("pass", "pass", elapsed_ms(start))
    except Exception as e:
        return ScenarioResult("fail", "pass", elapsed_ms(start), error=str(e))