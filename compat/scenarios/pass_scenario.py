"""Pass scenario — handler returns PASS sentinel.

Tests that when a handler returns PASS, no response is sent and the sender times out.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from layr8 import Client, Config, Message, PASS, log_errors

from .types import ScenarioContext, SenderContext, ScenarioResult, elapsed_ms

ECHO_TYPE = "https://layr8.test/echo/1.0/request"
ECHO_PROTOCOL = "https://layr8.test/echo/1.0"


async def run_receiver(
    ctx: ScenarioContext,
    on_ready: Callable[[str], None] | None = None,
) -> None:
    """Connect and register a handler that returns PASS. Blocks until cancelled."""
    client = Client(
        Config(
            node_url=ctx.node_url,
            api_key=ctx.api_key,
            agent_did=ctx.agent_did,
            protocols=[ECHO_PROTOCOL],
        ),
        log_errors(),
    )

    @client.handle(ECHO_TYPE)
    async def handler(msg: Message) -> Message | None:
        return PASS  # type: ignore[return-value]

    async with client:
        if on_ready:
            on_ready(client.did)
        await asyncio.Event().wait()


async def run_sender(ctx: SenderContext) -> ScenarioResult:
    """Send an echo request and expect a timeout because the receiver returns PASS."""
    client = Client(
        Config(
            node_url=ctx.node_url,
            api_key=ctx.api_key,
            agent_did=ctx.agent_did,
            protocols=[ECHO_PROTOCOL],
        ),
        log_errors(),
    )

    @client.handle(ECHO_TYPE)
    async def dummy_handler(msg: Message) -> Message | None:
        return None

    try:
        async with client:
            start = time.monotonic()
            try:
                await client.request(
                    Message(
                        type=ECHO_TYPE,
                        to=[ctx.receiver_did],
                        body={"ping": ctx.test_id},
                    ),
                    timeout=ctx.timeout,
                )
                return ScenarioResult(
                    "fail",
                    "pass",
                    elapsed_ms(start),
                    error="expected timeout but received a response",
                )
            except asyncio.TimeoutError:
                return ScenarioResult("pass", "pass", elapsed_ms(start))
    except Exception as e:
        return ScenarioResult("fail", "pass", elapsed_ms(start), error=str(e))