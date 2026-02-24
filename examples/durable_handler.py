"""
Durable Handler — persist messages to SQLite before acknowledging.

Demonstrates manual ack: messages are only acknowledged after they
are safely written to disk. If the process crashes between receive
and ack, the cloud-node redelivers the message.

Usage:
    LAYR8_API_KEY=your-key python examples/durable_handler.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal as signal_mod
import sqlite3

from layr8 import Client, Config, Message

DB_PATH = "messages.db"

logging.basicConfig(
    format="%(asctime)s.%(msecs)03d %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id       TEXT PRIMARY KEY,
            type     TEXT NOT NULL,
            from_did TEXT NOT NULL,
            body     TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


async def main() -> None:
    db = init_db()
    client = Client(Config())

    @client.handle("https://layr8.io/protocols/order/1.0/created", manual_ack=True)
    async def handle_order(msg: Message) -> None:
        body_json = json.dumps(msg.body) if msg.body else "{}"

        # Persist first — if this fails, the message is NOT acked
        # and the cloud-node will redeliver it.
        db.execute(
            "INSERT OR IGNORE INTO messages (id, type, from_did, body) VALUES (?, ?, ?, ?)",
            (msg.id, msg.type, msg.from_, body_json),
        )
        db.commit()

        msg.ack()  # safe to ack now
        log.info("persisted and acked message %s from %s", msg.id, msg.from_)
        return None

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal_mod.SIGINT, stop.set)

    async with client:
        log.info("durable handler running (DID=%s), persisting to %s", client.did, DB_PATH)
        await stop.wait()

    db.close()
    log.info("shutting down")


if __name__ == "__main__":
    asyncio.run(main())
