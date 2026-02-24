"""
Echo Agent — a deployable DIDComm echo service built with the Layr8 Python SDK.

Configuration via environment variables:
    LAYR8_NODE_URL  — WebSocket URL of the cloud-node
    LAYR8_API_KEY   — API key for authentication
    LAYR8_AGENT_DID — DID for this agent
    PEER_DIDS       — (optional) comma-separated DIDs to ping every 10s
    PEER_DID        — (optional) single DID to ping (backward compat)

Usage:
    LAYR8_NODE_URL=ws://charlie-test.localhost/plugin_socket/websocket \\
    LAYR8_API_KEY=charlie_ijkl9012_testkeycharltestkeycha24 \\
    LAYR8_AGENT_DID=did:web:charlie-test.localhost:sdk-echo-py \\
    PEER_DIDS=did:web:alice-test.localhost:sdk-echo-go,did:web:bob-test.localhost:sdk-echo-node \\
      python examples/echo_agent.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal as signal_mod
import time

from layr8 import Client, Config, Message

ECHO_PROTOCOL_BASE = "https://layr8.io/protocols/echo/1.0"
ECHO_REQUEST = f"{ECHO_PROTOCOL_BASE}/request"
ECHO_RESPONSE = f"{ECHO_PROTOCOL_BASE}/response"

logging.basicConfig(
    format="%(asctime)s.%(msecs)03d %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


def parse_peer_dids() -> list[str]:
    """Parse PEER_DIDS and PEER_DID env vars into a deduplicated list."""
    peers: list[str] = []
    dids = os.environ.get("PEER_DIDS", "")
    if dids:
        for d in dids.split(","):
            d = d.strip()
            if d:
                peers.append(d)
    # Backward compat: merge PEER_DID
    single = os.environ.get("PEER_DID", "")
    if single and single not in peers:
        peers.append(single)
    return peers


def short_did(did: str) -> str:
    """Return the last segment of a DID for compact log output."""
    parts = did.split(":")
    return parts[-1] if parts else did


async def ping_loop(
    client: Client,
    peer_did: str,
    stop_event: asyncio.Event,
) -> None:
    """Periodically ping a peer and log RTT."""
    # Wait for DID propagation across nodes
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=30)
        return  # stop_event was set
    except asyncio.TimeoutError:
        pass  # expected — propagation delay elapsed

    seq = 0
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=10)
            return
        except asyncio.TimeoutError:
            pass

        seq += 1
        msg_text = f"ping #{seq} from {client.did}"
        start = time.monotonic()

        try:
            resp = await client.request(
                Message(
                    type=ECHO_REQUEST,
                    to=[peer_did],
                    body={"message": msg_text},
                ),
                timeout=5.0,
            )
            rtt_ms = round((time.monotonic() - start) * 1000)
            body = resp.unmarshal_body()
            log.info(
                '[→ %s] ping #%d reply (%dms): "%s"',
                short_did(peer_did),
                seq,
                rtt_ms,
                body.get("echo", ""),
            )
        except Exception as exc:
            rtt_ms = round((time.monotonic() - start) * 1000)
            log.info(
                "[→ %s] ping #%d failed (%dms): %s",
                short_did(peer_did),
                seq,
                rtt_ms,
                exc,
            )


async def run_agent(stop_event: asyncio.Event) -> None:
    """Run a single agent session (reconnects on disconnect)."""
    client = Client(Config())

    inner_stop = asyncio.Event()

    @client.handle(ECHO_REQUEST)
    async def echo(msg: Message) -> Message:
        body = msg.unmarshal_body()
        log.info('echo request from %s: "%s"', msg.from_, body.get("message", ""))
        return Message(
            type=ECHO_RESPONSE,
            body={"echo": body.get("message", "")},
        )

    disconnect_event = asyncio.Event()

    def on_disconnect(err: Exception) -> None:
        inner_stop.set()
        disconnect_event.set()

    client.on_disconnect(on_disconnect)

    await client.connect()
    log.info("echo agent running (DID=%s)", client.did)

    ping_tasks: list[asyncio.Task[None]] = []
    for peer in parse_peer_dids():
        log.info("will ping %s every 10s", peer)
        task = asyncio.create_task(ping_loop(client, peer, inner_stop))
        ping_tasks.append(task)

    # Wait until disconnect or global stop
    done, _ = await asyncio.wait(
        [
            asyncio.create_task(disconnect_event.wait()),
            asyncio.create_task(stop_event.wait()),
        ],
        return_when=asyncio.FIRST_COMPLETED,
    )

    inner_stop.set()
    for task in ping_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await client.close()

    if disconnect_event.is_set() and not stop_event.is_set():
        raise ConnectionError("disconnected")


async def main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal_mod.SIGINT, stop_event.set)

    while not stop_event.is_set():
        try:
            await run_agent(stop_event)
        except Exception as exc:
            if stop_event.is_set():
                break
            log.info("disconnected: %s — reconnecting in 3s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=3)
                break
            except asyncio.TimeoutError:
                pass

    log.info("shutting down")


if __name__ == "__main__":
    asyncio.run(main())
