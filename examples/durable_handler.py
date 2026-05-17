"""
Durable Handler — persist messages to a file before returning.

Demonstrates durable processing: messages are only confirmed to the
cloud-node after the handler completes. If the process crashes mid-handler,
the cloud-node redelivers the message.

Messages are appended as JSON lines to messages.jsonl.

Usage:
    LAYR8_API_KEY=your-key python examples/durable_handler.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal as signal_mod

from layr8 import Client, Config, Message, log_errors

FILE_PATH = "messages.jsonl"

logging.basicConfig(
    format="%(asctime)s.%(msecs)03d %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


async def main() -> None:
    client = Client(Config(), log_errors())

    @client.handle("https://layr8.io/protocols/order/1.0/created")
    async def handle_order(msg: Message) -> None:
        record = json.dumps({
            "id": msg.id,
            "type": msg.type,
            "from": msg.from_,
            "body": msg.body,
        })

        # Persist first — if this crashes, the handler never returns,
        # so the cloud-node will redeliver the message.
        with open(FILE_PATH, "a") as f:
            f.write(record + "\n")
            f.flush()

        log.info("persisted message %s from %s", msg.id, msg.from_)
        return None

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal_mod.SIGINT, stop.set)

    async with client:
        log.info("durable handler running (DID=%s), persisting to %s", client.did, FILE_PATH)
        await stop.wait()

    log.info("shutting down")


if __name__ == "__main__":
    asyncio.run(main())
