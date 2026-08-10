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
from layr8 import Client, Config, Message, log_errors

client = Client(Config(
    node_url="ws://localhost:4000/plugin_socket/websocket",
    api_key="your-api-key",
    agent_did="did:web:myorg:my-agent",
), log_errors())

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
client = Client(Config(...), log_errors())

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
| `PASS` | Decline to handle — lets the cloud-node route elsewhere. |
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

For sender-only clients that use `request()` without registering handlers, specify protocols explicitly via `Config.protocols`:

```python
client = Client(Config(
    node_url="ws://localhost:4000/plugin_socket/websocket",
    api_key="my-key",
    protocols=["https://layr8.io/protocols/echo/1.0"],
), log_errors())
```

Config protocols are merged with handler-derived protocols (deduplicated). The cloud-node requires at least one protocol on join.

### Wildcard Handler

Register a catch-all for any message type not matched by a specific handler:

```python
from layr8 import PASS

@client.handle_all
async def catch_all(msg: Message) -> Message | None:
    if msg.type.startswith("https://myorg.com/"):
        return Message(type=msg.type + "/ack", body={"ok": True})
    return PASS  # decline — let the cloud-node route elsewhere
```

Dispatch priority: specific handler > catch-all > auto-pass to cloud-node.

## Sending Messages

### Send

Send a one-way message. By default, `send()` waits for the server to acknowledge receipt of the message. If the server rejects the message, a `RuntimeError` is raised.

```python
await client.send(Message(
    type="https://didcomm.org/basicmessage/2.0/message",
    to=["did:web:other-org:their-agent"],
    body={"content": "hello!"},
))
```

To skip waiting for the server acknowledgment (fire-and-forget), pass `fire_and_forget=True`:

```python
await client.send(
    Message(
        type="https://didcomm.org/basicmessage/2.0/message",
        to=["did:web:other-org:their-agent"],
        body={"content": "hello!"},
    ),
    fire_and_forget=True,
)
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
| `agent_did` | `LAYR8_AGENT_DID` | Yes | Agent DID identity |
| `protocols` | — | No | Additional protocol URIs to advertise on join |
| `attach_grants` | `LAYR8_ATTACH_GRANTS` | No | Attach Verifiable Grants to outbound messages. Default `True` |
| `grant_cache_ms` | `LAYR8_GRANT_CACHE_MS` | No | How long held grants are cached. Default `60_000` |
| `grant_read_timeout_ms` | `LAYR8_GRANT_READ_TIMEOUT_MS` | No | Deadline on the credential read. Default `2_000` |
| `rest_timeout_ms` | `LAYR8_REST_TIMEOUT_MS` | No | Deadline on every other REST call. Default `30_000`; `0` for none |
| `on_grant_miss` | — | No | Called when a grant was needed and not attached — see [Verifiable Grants](#verifiable-grants) |

`agent_did` is required — set it explicitly or via `LAYR8_AGENT_DID`. It's the DID your agent connects as and the address other agents use to message it; the cloud-node rejects a connection that doesn't specify one. Retrieve the active DID at runtime with `client.did`.

```python
# Explicit configuration
client = Client(Config(
    node_url="ws://localhost:4000/plugin_socket/websocket",
    api_key="my-api-key",
    agent_did="did:web:myorg:my-agent",
), log_errors())

# Environment-only configuration
# Set LAYR8_NODE_URL, LAYR8_API_KEY, LAYR8_AGENT_DID
client = Client(Config(), log_errors())
```

## Connection Lifecycle

### Agent DID

Your agent's DID is its identity on the network — the address other agents use to reach it. Configure it via `agent_did` (or the `LAYR8_AGENT_DID` env var); connecting without one is rejected by the cloud-node. Read the active DID back at runtime with `client.did`:

```python
client = Client(Config(
    node_url="ws://localhost:4000/plugin_socket/websocket",
    api_key="my-key",
    agent_did="did:web:myorg:my-agent",
), log_errors())
await client.connect()

print(client.did)  # "did:web:myorg:my-agent"
```

### Connection Resilience

The SDK automatically reconnects when the WebSocket connection drops (e.g., node restart, network interruption). Reconnection uses exponential backoff starting at 1 second, capped at 30 seconds.

During reconnection:
- `send()`, `request()`, and other operations raise `NotConnectedError` immediately — the SDK does not queue messages
- The `on_disconnect` callback fires when the connection drops
- The `on_reconnect` callback fires when the connection is restored
- `close()` stops the reconnect loop

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

### Error Handler (on_error)

The `Client` constructor requires an `on_error` callback as its second argument. This callback receives an `SDKError` for every SDK-level error that cannot be surfaced as an exception (parse failures, missing handlers, handler exceptions, server rejects, transport write errors).

```python
from layr8 import Client, Config, SDKError, ErrorKind, log_errors

# Use the built-in log_errors() helper for convenient logging
client = Client(Config(...), log_errors())

# Or provide a custom error handler
def my_error_handler(err: SDKError) -> None:
    print(f"SDK error [{err.kind.value}]: {err.cause}")

client = Client(Config(...), my_error_handler)
```

The `SDKError` dataclass contains:

| Field | Type | Description |
|---|---|---|
| `kind` | `ErrorKind` | Category of the error |
| `message_id` | `str` | ID of the message that caused the error (if available) |
| `type` | `str` | DIDComm message type (if available) |
| `from_did` | `str` | Sender DID (if available) |
| `cause` | `Exception \| None` | The underlying exception |
| `raw` | `Any` | Raw payload for parse failures |
| `timestamp` | `datetime` | When the error occurred (UTC) |

`ErrorKind` values:

| Kind | Description |
|---|---|
| `PARSE_FAILURE` | Inbound message could not be parsed as DIDComm |
| `NO_HANDLER` | No handler registered for the message type |
| `HANDLER_EXCEPTION` | A handler raised an exception |
| `SERVER_REJECT` | The server rejected a sent message |
| `TRANSPORT_WRITE` | Failed to write to the WebSocket transport |

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

## Verifiable Grants

The cloud-node requires a Verifiable Grant for anything its policy does not allow outright. **The SDK attaches the grants covering each outbound message automatically** — on `send()`, on `request()`, and on a handler's reply — so there is nothing to wire up. Turn it off with `Config(attach_grants=False)`.

Selection mirrors the policy and deliberately errs wide: everything that plausibly applies goes on the wire, because over-attaching is free (the policy allows on the first passing grant) while withholding one costs a working call and fails silently. Validity and revocation are the node's decision, not this side's.

```python
# A grant you were just given is invisible until the cache lapses (60s).
# If you have just been told you were granted something, say so:
client.refresh_grants()
```

### When a message goes out with nothing attached

The node's denial names the grant it could not find, which reads as "your grant is misconfigured" when the truth is "no credential was ever put on the wire". Only the sender knows which one it was. Wire `on_grant_miss` and the next such incident is one log line:

```python
def grant_miss(info: GrantMissInfo) -> None:
    logging.warning("grant miss: %s", info)

client = Client(Config(..., on_grant_miss=grant_miss), log_errors())
```

It fires in three cases, distinguished by which field is set:

| Field | Meaning |
|---|---|
| `denial_code` | The node denied a message we sent with **nothing attached** |
| `capped` | More grants covered the message than fit on it (`{"covering": n, "attached": 16}`) |
| `error` | The grants could not be **read** — every send after this is flying blind |

It deliberately does **not** fire merely because a message went out unattached: most traffic (discovery, trust-ping, problem reports) needs no grant, and a diagnostic that fires constantly is one nobody reads when it matters.

### Attaching one by hand

`media_type` is the only field the node's credential extractor filters on, by exact string equality, and it drops everything else **silently** — producing a denial byte-for-byte identical to the one for attaching nothing. Attach the credential **bare**; a Verifiable Presentation (`application/vp+jwt`) is dropped on that rule.

```python
Attachment(
    id="urn:uuid:…",
    media_type="application/vc+jwt",
    data=AttachmentData(jws=compact_jws),
)
```

## MCP (tool calling) over DIDComm

Layr8 services expose an MCP surface as DIDComm request/reply. `client.mcp()` removes the boilerplate — the protocol subscription, the type mapping (`tools/call` → `{base}/tools-call`), the JSON-RPC envelope, and unwrapping `result`.

It must be called **before** `connect()`, like `handle()`: it registers the protocol subscription the node needs in order to deliver replies.

```python
mcp = client.mcp()                     # default base: mcp/1.0
await client.connect()

loom = mcp.peer(loom_did)

await loom.initialize()
tools = await loom.list_tools()
result = await loom.call_tool("create_workflow", {"name": "onboarding"})
```

`McpError` is raised when the peer answers with a JSON-RPC `error`; a DIDComm-level failure — including an authorization denial — raises `ProblemReportError`, and an unanswered call raises `asyncio.TimeoutError`.

## Watching for changes (SpaceWatcher)

Nothing on the wire tells an SDK "your wallet changed" or "a resource came up", so both are polled. `SpaceWatcher` is the one place that loop lives, on semantics shared with every other Layr8 SDK.

```python
watcher = SpaceWatcher(
    fetch_wallet=lambda: list_my_grant_ids(),
    fetch_resources=lambda: list_mcp_instance_dids(),
    on_wallet_change=lambda wallet: rebuild_tools(),
    on_resources_change=lambda resources: rebuild_routes(resources),
)
await watcher.start()      # seeds both baselines silently
...
await watcher.refresh_wallet()   # pull the next check forward
await watcher.stop()
```

Neither callback fires on the first successful poll — a cold start is not a change. A fetch error never wipes state: it goes to `on_error` and the last-accepted value is retained, so a transient failure never reads as "everything disappeared". An empty *resource* result is only believed after two consecutive empty polls, since a directory answering with nothing is as likely to be a keepalive blip as a real teardown; an empty *wallet* is believed immediately, because that is a real answer.

## W3C Verifiable Credentials

The SDK provides methods for signing, verifying, storing, listing, and retrieving [W3C Verifiable Credentials](https://www.w3.org/TR/vc-data-model-2.0/). These operations use the cloud-node's REST API and the DID keys in the node's wallet.

### Sign a Credential

```python
from layr8.credentials import Credential

cred = Credential(
    context=["https://www.w3.org/ns/credentials/v2"],
    id="urn:uuid:my-credential",
    type=["VerifiableCredential"],
    issuer=client.did,
    credential_subject={"id": "did:web:example:holder", "name": "Alice"},
)

signed_jwt = await client.sign_credential(cred)
```

Keyword arguments: `issuer_did`, `format`.

### Verify a Credential

```python
verified = await client.verify_credential(signed_jwt)
print(verified.credential)  # decoded credential claims
print(verified.headers)      # JWT headers (alg, kid, etc.)
```

Keyword arguments: `verifier_did`.

> **Note:** The verifier DID must have keys in the local node's wallet. Cross-node verification is not currently supported.

### Store, List, Get

```python
# Store a signed credential
stored = await client.store_credential(signed_jwt)
print(stored.id)  # storage ID

# List all stored credentials
creds = await client.list_credentials()

# Retrieve by ID
fetched = await client.get_credential(stored.id)
print(fetched.credential_jwt)  # the original signed JWT
```

Store keyword arguments: `holder_did`, `issuer_did`, `valid_until`.
List keyword arguments: `holder_did`.

### Output Formats

The `format` argument accepts: `"compact_jwt"` (default), `"json"`, `"jwt"`, `"enveloped"`.

## W3C Verifiable Presentations

Presentations wrap one or more signed credentials into a holder-signed envelope.

> **A presentation is not how you authorize a message.** The node keeps only attachments whose `media_type` is exactly `application/vc+jwt` and drops a `vp+jwt` silently — an identical denial to attaching nothing. Attach the credential bare, or let the SDK do it, which it does by default. See [Verifiable Grants](#verifiable-grants).

### Sign a Presentation

```python
signed_pres = await client.sign_presentation(
    [signed_jwt],
    nonce="challenge-from-verifier",
)
```

Keyword arguments: `holder_did`, `format`, `nonce`.

### Verify a Presentation

```python
verified = await client.verify_presentation(signed_pres)
print(verified.presentation)  # decoded presentation claims
print(verified.headers)        # JWT headers
```

Keyword arguments: `verifier_did`.

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

### Durable Handler

Persist-then-return pattern for durable processing: writes inbound messages to a JSON-lines file before returning. If the process crashes mid-handler, the cloud-node redelivers the message.

```bash
LAYR8_API_KEY=your-key python examples/durable_handler.py
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

### Compatibility Testing

The `compat/` directory contains the compatibility test suite — scenarios that validate the SDK against real cloud-node versions and cross-language interoperability with other Layr8 SDKs.

**Architecture:** The compat suite uses a hexagonal architecture:

- **Scenarios** (`compat/scenarios/`) — pure async Python, no framework dependencies. Each scenario exposes `run_receiver(ctx, on_ready=)` and `run_sender(ctx)` functions.
- **Layer 1** (`compat/tests/`) — pytest + testcontainers adapter. Spins up real cloud-node Docker containers and runs scenarios against them.
- **Layer 2** (`compat/bin/compat.py`) — CLI adapter for the compat-suite orchestrator. Implements the standard interface: `--mode sender|receiver --scenario <name> --node <url> --did <did>`.

```bash
# Unit tests (mock server, no Docker needed)
cd compat && pytest tests/ -v --ignore=tests/conftest.py

# Layer 1 integration tests (requires Docker)
cd compat && pip install -e ".[test]" && pytest tests/ -v
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
