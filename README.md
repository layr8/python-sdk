# Layr8 Python SDK

The official Python SDK for building agents on the [Layr8](https://layr8.com) platform. Agents connect to Layr8 cloud-nodes via WebSocket and exchange [DIDComm v2](https://identity.foundation/didcomm-messaging/spec/) messages with other agents across the network.

## Installation

```bash
pip install layr8
```

Requires Python 3.11 or later.

## Quick Start

```python
import asyncio
from layr8 import Client, Config, Message

client = Client(Config(
    node_url="ws://localhost:4000/plugin_socket/websocket",
    api_key="your-api-key",
    agent_did="did:web:myorg:my-agent",
))

@client.handle("https://layr8.io/protocols/echo/1.0/request")
async def echo(msg: Message) -> Message:
    body = msg.unmarshal_body()
    return Message(
        type="https://layr8.io/protocols/echo/1.0/response",
        body={"echo": body["message"]},
    )

async def main():
    async with client:
        print(f"agent running as {client.did}")
        await asyncio.Event().wait()

asyncio.run(main())
```

## Core Concepts

### Client

The `Client` is the main entry point. It manages the WebSocket connection to a cloud-node, routes inbound messages to handlers, and provides methods for sending outbound messages.

```python
client = Client(Config(...))

# Register handlers before connecting
@client.handle(message_type)
async def handler(msg: Message) -> Message | None:
    ...

# Connect to the cloud-node
await client.connect()
```

The client supports `async with` for automatic connect/close:

```python
async with client:
    # connected here
    ...
# automatically closed
```

### Messages

`Message` is a dataclass representing a DIDComm v2 message:

```python
@dataclass
class Message:
    id: str = ""                # unique message ID (auto-generated if empty)
    type: str = ""              # DIDComm message type URI
    from_: str = ""             # sender DID (auto-filled from client, wire: "from")
    to: list[str]               # recipient DIDs
    thread_id: str = ""         # thread correlation ID
    parent_thread_id: str = ""  # parent thread for nested conversations
    body: Any = None            # message payload (serialized to JSON)
    context: MessageContext | None = None  # cloud-node metadata (inbound only)
```

> **Note:** The `from` field is named `from_` because `from` is a Python reserved word. It serializes as `"from"` on the wire.

Decode the body of an inbound message with `unmarshal_body`:

```python
# As a dict
body = msg.unmarshal_body()

# As a typed dataclass
@dataclass
class MyRequest:
    message: str

body = msg.unmarshal_body(MyRequest)
print(body.message)  # typed attribute access
```

### Handlers

Handlers process inbound messages. Register them with `@client.handle()` before calling `connect()`.

A handler receives a `Message` and returns:

| Return value | Behavior |
|---|---|
| `Message(...)` | Sends response to the sender. `from_`, `to`, and `thread_id` are auto-filled. |
| `None` | Fire-and-forget — no response sent. |
| Raised exception | Sends a DIDComm [problem report](https://identity.foundation/didcomm-messaging/spec/#problem-reports) to the sender. |

```python
@client.handle("https://layr8.io/protocols/echo/1.0/request")
async def echo(msg: Message) -> Message:
    body = msg.unmarshal_body()
    return Message(
        type="https://layr8.io/protocols/echo/1.0/response",
        body={"echo": body["message"]},
    )
```

Handlers can also be registered with a direct call:

```python
client.handle("https://layr8.io/protocols/echo/1.0/request", echo_handler)
```

#### Protocol Registration

The SDK automatically derives protocol base URIs from your handler message types and registers them with the cloud-node on connect. For example, handling `https://layr8.io/protocols/echo/1.0/request` registers the protocol `https://layr8.io/protocols/echo/1.0`.

## Sending Messages

### Send (Fire-and-Forget)

Send a one-way message with no response expected:

```python
await client.send(Message(
    type="https://didcomm.org/basicmessage/2.0/message",
    to=["did:web:other-org:their-agent"],
    body={"content": "hello!"},
))
```

### Request (Request/Response)

Send a message and await a correlated response:

```python
resp = await client.request(
    Message(
        type="https://layr8.io/protocols/echo/1.0/request",
        to=["did:web:other-org:echo-agent"],
        body={"message": "ping"},
    ),
    timeout=5.0,
)

body = resp.unmarshal_body()
print(body["echo"])  # "ping"
```

Thread correlation is automatic — the SDK generates a `thread_id`, attaches it to the outbound message, and matches the inbound response by the same `thread_id`.

#### Request Options

```python
# Set parent thread ID for nested conversations
resp = await client.request(msg, parent_thread="parent-thread-id", timeout=10.0)
```

## Configuration

Configuration can be set explicitly or via environment variables. Environment variables are used as fallbacks when the corresponding field is empty.

| Field | Environment Variable | Required | Description |
|---|---|---|---|
| `node_url` | `LAYR8_NODE_URL` | Yes | WebSocket URL of the cloud-node |
| `api_key` | `LAYR8_API_KEY` | Yes | API key for authentication |
| `agent_did` | `LAYR8_AGENT_DID` | No | Agent DID identity |

If `agent_did` is not provided, the cloud-node creates an ephemeral DID on connect. Retrieve it with `client.did`.

```python
# Explicit configuration
client = Client(Config(
    node_url="ws://localhost:4000/plugin_socket/websocket",
    api_key="my-api-key",
    agent_did="did:web:myorg:my-agent",
))

# Environment-only configuration
# Set LAYR8_NODE_URL, LAYR8_API_KEY, LAYR8_AGENT_DID
client = Client(Config())
```

## Handler Options

### Manual Acknowledgment

By default, messages are acknowledged to the cloud-node before the handler runs (auto-ack). For handlers where you need guaranteed processing, use manual ack to acknowledge only after successful execution. Unacknowledged messages are redelivered by the cloud-node.

```python
@client.handle(query_type, manual_ack=True)
async def handle_query(msg: Message) -> Message:
    result = await execute_query(msg)
    msg.ack()  # explicitly acknowledge after success
    return Message(type=result_type, body=result)
```

## Connection Lifecycle

### DID Assignment

If no `agent_did` is configured, the cloud-node assigns an ephemeral DID on connect:

```python
client = Client(Config(
    node_url="ws://localhost:4000/plugin_socket/websocket",
    api_key="my-key",
))
await client.connect()

print(client.did)  # "did:web:myorg:abc123" (assigned by node)
```

### Disconnect and Reconnect Callbacks

Monitor connection state with callbacks:

```python
@client.on_disconnect
def handle_disconnect(err: Exception):
    print(f"disconnected: {err}")

@client.on_reconnect
def handle_reconnect():
    print("reconnected")
```

## Message Context

Inbound messages include a `context` field with metadata from the cloud-node:

```python
@client.handle(message_type)
async def handler(msg: Message) -> None:
    if msg.context:
        print("Recipient:", msg.context.recipient)
        print("Authorized:", msg.context.authorized)

        for cred in msg.context.sender_credentials:
            print(f"Sender credential: {cred.name} ({cred.id})")
    return None
```

| Field | Type | Description |
|---|---|---|
| `recipient` | `str` | The DID that received this message |
| `authorized` | `bool` | Whether the sender is authorized by the node's policy |
| `sender_credentials` | `list[Credential]` | Verifiable credentials presented by the sender |

## Error Handling

### Problem Reports

When a handler raises an exception, the SDK automatically sends a [DIDComm problem report](https://identity.foundation/didcomm-messaging/spec/#problem-reports) to the sender:

```python
@client.handle(msg_type)
async def handler(msg: Message) -> None:
    raise RuntimeError("something went wrong")  # sends problem report
```

When `request()` receives a problem report as the response, it raises a `ProblemReportError`:

```python
from layr8 import ProblemReportError

try:
    resp = await client.request(msg)
except ProblemReportError as e:
    print(f"Remote error [{e.code}]: {e.comment}")
```

### Connection Errors

Connection failures raise a `Layr8ConnectionError`:

```python
from layr8.errors import Layr8ConnectionError

try:
    await client.connect()
except Layr8ConnectionError as e:
    print(f"Failed to connect to {e.url}: {e.reason}")
```

### Error Classes

| Error | Description |
|---|---|
| `NotConnectedError` | Operation attempted before `connect()` or after `close()` |
| `AlreadyConnectedError` | `handle()` called after `connect()` |
| `ClientClosedError` | `connect()` called on a closed client |
| `ProblemReportError` | Remote handler returned an error (`.code`, `.comment`) |
| `Layr8ConnectionError` | Failed to connect to cloud-node (`.url`, `.reason`) |

## Examples

The [examples/](examples/) directory contains complete, runnable agents:

### Echo Agent

A minimal agent that echoes back any message it receives. Demonstrates request/response handlers with auto-ack, auto-thread correlation, and reconnection with backoff.

```bash
LAYR8_API_KEY=your-key python examples/echo_agent.py
```

### Chat Client

An interactive chat client for DIDComm basic messaging. Demonstrates fire-and-forget `send()`, inbound message handling, `MessageContext` for sender credentials, and multi-recipient messaging.

```bash
LAYR8_API_KEY=your-key python examples/chat.py did:web:friend:chat-agent
```

## Development

### Prerequisites

- Python 3.11+

### Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Running Tests

```bash
pytest           # Run all tests
pytest -v        # Verbose output
```

## Architecture

The SDK is structured around a small set of types:

```
Client            → public API (connect, send, request, handle, close)
  ├── Config      → configuration with env var fallback
  ├── Message     → DIDComm v2 message envelope (dataclass)
  ├── Handler     → message type → handler function registry
  └── Channel     → WebSocket/Phoenix Channel transport
```

The transport layer implements the Phoenix Channel V2 wire protocol over WebSocket, including join negotiation, heartbeats, and message acknowledgment.

## License

Copyright Layr8 Inc. All rights reserved.
