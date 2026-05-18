"""Disconnected scenario — message to an offline agent.

Tests that sending a message to a DID with no connected agent
results in a clean timeout, not a crash or hang.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from layr8 import Client, Config, Message, log_errors

from .types import ScenarioContext, SenderContext, ScenarioResult, elapsed_ms

DISCONNECTED_TYPE = "https://layr8.test/disconnected/1.0/request"
DISCONNECTED_PROTOCOL = "https://layr8.test/disconnected/1.0"


async def run_receiver(
    ctx: ScenarioContext,
    on_ready: Callable[[str], None] | None = None,
) -> None:
    """Connect to the cloud-node and wait until killed."""
    client = Client(
        Config(
            node_url=ctx.node_url,
            api_key=ctx.api_key,
            agent_did=ctx.agent_did,
            protocols=[DISCONNECTED_PROTOCOL],
        ),
        log_errors(),
    )

    async with client:
        if on_ready:
            on_ready(client.did)
        await asyncio.Event().wait()


async def run_sender(ctx: SenderContext) -> ScenarioResult:
    """Send to a non-existent DID and verify clean timeout."""
    client = Client(
        Config(
            node_url=ctx.node_url,
            api_key=ctx.api_key,
            agent_did=ctx.agent_did,
            protocols=[DISCONNECTED_PROTOCOL],
        ),
        log_errors(),
    )
    start = time.monotonic()

    try:
        async with client:
            try:
                await client.request(
                    Message(
                        type=DISCONNECTED_TYPE,
                        to=[ctx.receiver_did],
                        body={"test_id": ctx.test_id},
                    ),
                    timeout=ctx.timeout,
                )
                return ScenarioResult(
                    "fail", "disconnected", elapsed_ms(start),
                    error="expected timeout but got response",
                )
            except asyncio.TimeoutError:
                return ScenarioResult("pass", "disconnected", elapsed_ms(start))
    except Exception as e:
        return ScenarioResult(
            "fail", "disconnected", elapsed_ms(start), error=str(e),
        )