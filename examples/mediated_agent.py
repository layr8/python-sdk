"""
Mediated Agent — an agent that collects what it missed while offline.

Give the client a mediator (a `layr8/mediator` in your Space) and, on every
connect and reconnect, it enrols, declares the mediator on its own node,
collects everything queued while it was away and turns live delivery on — in
the background. Collected messages arrive through the ordinary handlers.

Configuration via environment variables:
    LAYR8_NODE_URL      — WebSocket URL of the cloud-node
    LAYR8_API_KEY       — API key for authentication
    LAYR8_AGENT_DID     — DID for this agent
    LAYR8_MEDIATOR_DID  — the mediator's DID (discover it by tag "mediator")
    LAYR8_MEDIATOR_LIVE — "false" collects but leaves live delivery off

Usage:
    LAYR8_MEDIATOR_DID=did:web:node.example:mediator python examples/mediated_agent.py
"""

from __future__ import annotations

import asyncio
import logging
import signal as signal_mod

from layr8 import Client, Config, ErrorKind, Message, SDKError, log_errors, mediation

BASICMESSAGE = "https://didcomm.org/basicmessage/2.0/message"

logging.basicConfig(format="%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S", level=logging.INFO)
log = logging.getLogger(__name__)


async def main() -> None:
    fallback = log_errors()

    def on_error(err: SDKError) -> None:
        # A background mediation step that failed names the step in `type`.
        if err.kind is ErrorKind.MEDIATION:
            log.warning("mediation %s failed against %s: %s", err.type, err.from_did, err.cause)
        else:
            fallback(err)

    client = Client(Config(), on_error)  # mediator from LAYR8_MEDIATOR_DID

    @client.handle(BASICMESSAGE)
    async def on_message(msg: Message) -> None:
        body = msg.unmarshal_body()
        log.info("from %s: %s", msg.from_, body.get("content", body))
        return None

    async with client:
        log.info("connected as %s; mediator %s", client.did, client.mediator)
        if client.mediator:
            s = await mediation.status(client, client.mediator)
            log.info("mediator status: %s", s.status if s.ok else s.error)

        stop = asyncio.Event()
        asyncio.get_running_loop().add_signal_handler(signal_mod.SIGINT, stop.set)
        await stop.wait()


if __name__ == "__main__":
    asyncio.run(main())
