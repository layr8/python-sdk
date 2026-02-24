---
name: build-layr8-agent
description: Use when building a Python agent for the Layr8 platform. Covers the full SDK API — config, handlers, messaging, error handling, and DIDComm conventions.
---

# Building Layr8 Agents with the Python SDK

Full documentation: https://docs.layr8.io/reference/python-sdk

## Import

```python
from layr8 import Client, Config, Message
```

Requires Python 3.11+. The SDK is fully async, built on `asyncio` and `websockets`.

## Config

```python
client = Client(Config(
    node_url="ws://mynode.localhost/plugin_socket/websocket",
    api_key="my_api_key",
    agent_did="did:web:mynode.localhost:my-agent",
))
```

All fields fall back to environment variables if empty:
- `node_url`  → `LAYR8_NODE_URL`
- `api_key`   → `LAYR8_API_KEY`
- `agent_did` → `LAYR8_AGENT_DID`

`agent_did` is optional — if omitted, the node assigns an ephemeral DID on connect.

## Lifecycle

```
Client(Config) → handle (register handlers) → connect → ... → close
```

Or using the async context manager:

```
Client(Config) → handle → async with client: ...
```

- `handle` must be called BEFORE `connect` — raises `AlreadyConnectedError` after.
- `connect()` establishes WebSocket and joins the Phoenix Channel. Returns a coroutine.
- `close()` sends `phx_leave` and shuts down gracefully. Returns a coroutine.
- `did` (property) returns the agent's DID (explicit or node-assigned).
- Supports `async with` context manager (`__aenter__`/`__aexit__`).

## Registering Handlers

### Decorator Syntax

```python
@client.handle("https://layr8.io/protocols/echo/1.0/request")
async def echo(msg: Message) -> Message:
    body = msg.unmarshal_body()
    return Message(
        type="https://layr8.io/protocols/echo/1.0/response",
        body={"echo": body["message"]},
    )
```

### Direct Call

```python
async def echo(msg: Message) -> Message:
    body = msg.unmarshal_body()
    return Message(
        type="https://layr8.io/protocols/echo/1.0/response",
        body={"echo": body["message"]},
    )

client.handle("https://layr8.io/protocols/echo/1.0/request", echo)
```

Handler return values:
- `Message(...)` → send response to sender (auto-fills `id`, `from_`, `to`, `thread_id`)
- `None` → no response (fire-and-forget inbound)
- Raised exception → send DIDComm problem report to sender

The protocol base URI is derived automatically from the message type
(last path segment removed) and registered with the node on connect.

## Sending Messages

### Fire-and-forget

```python
await client.send(Message(
    type="https://didcomm.org/basicmessage/2.0/message",
    to=["did:web:other-node:agent"],
    body={"content": "Hello!"},
))
```

### Request/Response

```python
resp = await client.request(
    Message(
        type="https://layr8.io/protocols/echo/1.0/request",
        to=["did:web:other-node:agent"],
        body={"message": "ping"},
    ),
    timeout=5.0,
)

body = resp.unmarshal_body()
# resp is the correlated response (matched by thread ID)
```

## Message Structure

```python
@dataclass
class Message:
    id: str = ""                        # auto-generated if empty
    type: str = ""                      # DIDComm message type URI
    from_: str = ""                     # auto-filled with agent DID (wire: "from")
    to: list[str] = field(default_factory=list)  # recipient DIDs
    thread_id: str = ""                 # auto-generated for request (wire: "thid")
    parent_thread_id: str = ""          # set via parent_thread param (wire: "pthid")
    body: Any = None                    # serialized to JSON
    context: MessageContext | None = None  # populated on inbound messages
```

**Important:** The `from` field is named `from_` because `from` is a Python reserved word. On the wire, it serializes as `"from"`.

### Inbound Message Context

```python
if msg.context:
    msg.context.authorized           # bool — node authorization result
    msg.context.recipient            # str — recipient DID
    msg.context.sender_credentials   # list[Credential] — each has .id, .name
```

### Unmarshaling Body

```python
# As a dict
body = msg.unmarshal_body()

# As a typed dataclass
@dataclass
class EchoRequest:
    message: str

body = msg.unmarshal_body(EchoRequest)
print(body.message)  # typed attribute access
```

## Options

### Manual Ack

By default, messages are auto-acked before the handler runs.
Use `manual_ack` to control ack timing (e.g., ack only after DB write):

```python
@client.handle(msg_type, manual_ack=True)
async def handler(msg: Message) -> Message:
    result = process(msg)
    msg.ack()  # explicitly ack after processing
    return Message(type=result_type, body=result)
```

### Parent Thread

For nested thread correlation:

```python
resp = await client.request(msg, parent_thread="parent-thread-id", timeout=10.0)
```

## Error Handling

### Problem Reports

When a remote handler raises, `request` raises `ProblemReportError`:

```python
from layr8 import ProblemReportError

try:
    resp = await client.request(msg)
except ProblemReportError as e:
    print(f"remote error [{e.code}]: {e.comment}")
```

### Error Classes

- `NotConnectedError` — `send`/`request` called before `connect`
- `AlreadyConnectedError` — `handle` called after `connect`
- `ClientClosedError` — `connect` called after `close`
- `Layr8ConnectionError` — failed to connect to node (`.url`, `.reason`)

```python
from layr8.errors import Layr8ConnectionError

try:
    await client.connect()
except Layr8ConnectionError as e:
    print(f"failed to connect to {e.url}: {e.reason}")
```

Note: `request()` raises `asyncio.TimeoutError` on timeout (uses `asyncio.wait_for`).

## Connection Callbacks

```python
@client.on_disconnect
def handle_disconnect(err: Exception):
    print(f"connection lost: {err}")

@client.on_reconnect
def handle_reconnect():
    print("reconnected")
```

Note: `on_disconnect` fires only on unexpected drops, not on `close()`.

## DID and Protocol Conventions

### DID Format

```
did:web:{node-domain}:{agent-path}
```

Examples:
- `did:web:alice-test.localhost:my-agent`
- `did:web:earth.node.layr8.org:echo-service`

### Protocol URI Format

```
https://layr8.io/protocols/{name}/{version}/{message-type}
```

The base URI (without the last segment) is the protocol identifier.
Example: `https://layr8.io/protocols/echo/1.0/request` → protocol `https://layr8.io/protocols/echo/1.0`

### Standard Protocols

- Basic message: `https://didcomm.org/basicmessage/2.0/message`
- Problem report: `https://didcomm.org/report-problem/2.0/problem-report`

## Complete Example: Echo Agent

```python
import asyncio
from layr8 import Client, Config, Message

ECHO_REQUEST = "https://layr8.io/protocols/echo/1.0/request"
ECHO_RESPONSE = "https://layr8.io/protocols/echo/1.0/response"

client = Client(Config())

@client.handle(ECHO_REQUEST)
async def echo(msg: Message) -> Message:
    body = msg.unmarshal_body()
    return Message(
        type=ECHO_RESPONSE,
        body={"echo": body["message"]},
    )

async def main():
    async with client:
        print(f"echo agent running as {client.did}")
        await asyncio.Event().wait()

asyncio.run(main())
```

## Complete Example: Request/Response Client

```python
import asyncio
from layr8 import Client, Config, Message, ProblemReportError

ECHO_REQUEST = "https://layr8.io/protocols/echo/1.0/request"

client = Client(Config())

# Must register the protocol even if not handling inbound
@client.handle(ECHO_REQUEST)
async def noop(msg: Message) -> None:
    return None

async def main():
    async with client:
        try:
            resp = await client.request(
                Message(
                    type=ECHO_REQUEST,
                    to=["did:web:other-node:echo-agent"],
                    body={"message": "Hello!"},
                ),
                timeout=5.0,
            )

            body = resp.unmarshal_body()
            print(f"response: {body['echo']}")
        except ProblemReportError as e:
            print(f"remote error [{e.code}]: {e.comment}")
        except asyncio.TimeoutError:
            print("request timed out")

asyncio.run(main())
```

## More Examples

See the `examples/` directory in the SDK repo for complete working agents:
- `examples/echo_agent.py` — minimal echo service
- `examples/chat.py` — interactive chat client
- `examples/durable_handler.py` — persist-then-ack with JSON-lines
