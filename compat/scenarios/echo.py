"""Echo scenario — basic request/response messaging."""

from __future__ import annotations

import asyncio
import time

from layr8 import Client, Config, Message, log_errors

from .types import ScenarioContext, SenderContext, ScenarioResult, elapsed_ms

ECHO_TYPE = "https://layr8.test/echo/1.0/request"
ECHO_RESPONSE_TYPE = "https://layr8.test/echo/1.0/response"


async def run_receiver(ctx: ScenarioContext) -> None:
    """Connect and register echo handler. Blocks until cancelled."""
    client = Client(
        Config(node_url=ctx.node_url, api_key=ctx.api_key),
        log_errors(),
    )

    @client.handle(ECHO_TYPE)
    async def handler(msg: Message) -> Message:
        body = msg.unmarshal_body()
        return Message(
            type=ECHO_RESPONSE_TYPE,
            body={"echo": body, "from": client.did},
        )

    async with client:
        await asyncio.Event().wait()


async def run_sender(ctx: SenderContext) -> ScenarioResult:
    """Send an echo request and verify the response."""
    client = Client(
        Config(node_url=ctx.node_url, api_key=ctx.api_key),
        log_errors(),
    )
    start = time.monotonic()

    try:
        async with client:
            resp = await client.request(
                Message(
                    type=ECHO_TYPE,
                    to=[ctx.receiver_did],
                    body={"ping": ctx.test_id},
                ),
                timeout=ctx.timeout,
            )
            body = resp.unmarshal_body()
            echo = body.get("echo", {})
            if isinstance(echo, dict) and echo.get("ping") == ctx.test_id:
                return ScenarioResult("pass", "echo", elapsed_ms(start))
            return ScenarioResult(
                "fail", "echo", elapsed_ms(start),
                error=f"unexpected echo: {echo!r}",
            )
    except Exception as e:
        return ScenarioResult("fail", "echo", elapsed_ms(start), error=str(e))