"""
Chat Client — a simple DIDComm chat using the Layr8 Python SDK.

Demonstrates: Send (fire-and-forget), Handle (decorator), MessageContext,
multi-recipient, async context manager.

Usage:
    LAYR8_NODE_URL=wss://earth.node.layr8.org:443/plugin_socket/websocket \\
    LAYR8_AGENT_DID=did:web:earth:my-agent \\
    LAYR8_API_KEY=my-key \\
      python examples/chat.py did:web:other-node:agent
"""

from __future__ import annotations

import asyncio
import signal as signal_mod
import sys

from layr8 import Client, Config, Message, log_errors


async def main() -> None:
    recipients = sys.argv[1:]
    if not recipients:
        print("usage: chat <recipient-did> [recipient-did...]", file=sys.stderr)
        sys.exit(1)

    client = Client(Config(), log_errors())

    @client.handle("https://didcomm.org/basicmessage/2.0/message")
    async def on_message(msg: Message) -> None:
        body = msg.unmarshal_body()
        sender = msg.from_
        if msg.context and msg.context.sender_credentials:
            sender = msg.context.sender_credentials[0].name
        print(f"[{sender}] {body.get('content', '')}")
        return None

    @client.handle("https://didcomm.org/report-problem/2.0/problem-report")
    async def on_problem(msg: Message) -> None:
        body = msg.unmarshal_body()
        print(f"server: [{body.get('code', '')}] {body.get('comment', '')}")
        return None

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal_mod.SIGINT, stop_event.set)

    client.on_disconnect(lambda _: print("--- disconnected ---"))
    client.on_reconnect(lambda: print("--- reconnected ---"))

    async with client:
        print(f"chatting with {', '.join(recipients)}")
        print("type a message and press enter (Ctrl+C to quit)")

        reader = asyncio.StreamReader()
        protocol = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )

        while not stop_event.is_set():
            try:
                line_bytes = await asyncio.wait_for(reader.readline(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            if not line_bytes:
                break

            text = line_bytes.decode().strip()
            if not text:
                continue

            try:
                await client.send(
                    Message(
                        type="https://didcomm.org/basicmessage/2.0/message",
                        to=recipients,
                        body={"content": text, "locale": "en"},
                    )
                )
            except Exception as exc:
                print(f"send error: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
