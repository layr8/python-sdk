# Reply Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add dispatch_reply protocol, wildcard binding, capability negotiation, and PASS sentinel to the Python SDK (LAYR8-610).

**Architecture:** Flag-driven branching — after join, a `reply_protocol` bool on the channel selects new-mode (dispatch_reply) or legacy-mode (ack). Handler registry gains a catch-all slot. PASS sentinel is a singleton object handlers return to decline a message.

**Tech Stack:** Python 3.11+, asyncio, websockets, pytest

---

### Task 1: PASS Sentinel

**Files:**
- Create: `src/layr8/sentinel.py`
- Modify: `src/layr8/__init__.py`
- Create: `tests/test_sentinel.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sentinel.py
"""Tests for layr8.sentinel."""

from __future__ import annotations


class TestPassSentinel:
    def test_is_singleton(self) -> None:
        from layr8.sentinel import _Pass
        a = _Pass()
        b = _Pass()
        assert a is b

    def test_repr(self) -> None:
        from layr8.sentinel import PASS
        assert repr(PASS) == "PASS"

    def test_is_falsy(self) -> None:
        from layr8.sentinel import PASS
        assert not PASS
        assert bool(PASS) is False

    def test_is_not_none(self) -> None:
        from layr8.sentinel import PASS
        assert PASS is not None

    def test_importable_from_package(self) -> None:
        from layr8 import PASS
        assert repr(PASS) == "PASS"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sentinel.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'layr8.sentinel'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/layr8/sentinel.py
"""PASS sentinel for handler dispatch."""

from __future__ import annotations


class _Pass:
    """Sentinel returned by handlers to signal 'I don't handle this'."""

    _instance: _Pass | None = None

    def __new__(cls) -> _Pass:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "PASS"

    def __bool__(self) -> bool:
        return False


PASS = _Pass()
```

Add to `src/layr8/__init__.py` — add the import and export:

```python
from .sentinel import PASS
```

Add `"PASS"` to the `__all__` list.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sentinel.py -v`
Expected: all 5 PASS

- [ ] **Step 5: Run full test suite**

Run: `pytest --tb=short`
Expected: all existing tests still pass

- [ ] **Step 6: Commit**

```
git add src/layr8/sentinel.py tests/test_sentinel.py src/layr8/__init__.py
git commit -m "Add PASS sentinel for handler dispatch"
```

---

### Task 2: Handler Registry — Remove manual_ack

**Files:**
- Modify: `src/layr8/handler.py`
- Modify: `tests/test_handler.py`

- [ ] **Step 1: Update existing test to remove manual_ack assertion**

In `tests/test_handler.py`, delete the `test_register_with_manual_ack` test entirely. In `test_register_and_lookup`, remove the `assert entry.manual_ack is False` line.

- [ ] **Step 2: Run tests to verify they still pass**

Run: `pytest tests/test_handler.py -v`
Expected: all pass (one fewer test)

- [ ] **Step 3: Remove manual_ack from HandlerEntry and register()**

In `src/layr8/handler.py`:

Remove `manual_ack: bool = False` from `HandlerEntry`.

Remove the `manual_ack` parameter from `register()` and the `manual_ack=manual_ack` in the `HandlerEntry` constructor call:

```python
@dataclass
class HandlerEntry:
    fn: HandlerFn


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, HandlerEntry] = {}

    def register(
        self,
        msg_type: str,
        fn: HandlerFn,
    ) -> None:
        if msg_type in self._handlers:
            raise ValueError(
                f'handler already registered for message type "{msg_type}"'
            )
        self._handlers[msg_type] = HandlerEntry(fn=fn)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_handler.py -v`
Expected: all pass

- [ ] **Step 5: Run full test suite to find breakages**

Run: `pytest --tb=short`
Expected: failures in `test_client.py` where `manual_ack=True` is used. That's expected — we'll fix client.py in Task 5.

- [ ] **Step 6: Commit**

```
git add src/layr8/handler.py tests/test_handler.py
git commit -m "Remove manual_ack from handler registry"
```

---

### Task 3: Handler Registry — Add catch-all and wildcard protocol

**Files:**
- Modify: `src/layr8/handler.py`
- Modify: `tests/test_handler.py`

- [ ] **Step 1: Write failing tests for catch-all**

Append to `tests/test_handler.py`:

```python
class TestCatchAll:
    def test_register_catch_all(self) -> None:
        registry = HandlerRegistry()
        registry.register_catch_all(noop_handler)
        entry = registry.lookup("https://any.org/protocol/1.0/anything")
        assert entry is not None
        assert entry.fn is noop_handler

    def test_specific_handler_takes_priority(self) -> None:
        registry = HandlerRegistry()

        async def specific(msg: Message) -> None:
            return None

        registry.register("https://layr8.io/protocols/echo/1.0/request", specific)
        registry.register_catch_all(noop_handler)

        entry = registry.lookup("https://layr8.io/protocols/echo/1.0/request")
        assert entry is not None
        assert entry.fn is specific

    def test_catch_all_used_when_no_specific(self) -> None:
        registry = HandlerRegistry()
        registry.register("https://layr8.io/protocols/echo/1.0/request", noop_handler)
        registry.register_catch_all(noop_handler)

        entry = registry.lookup("https://unknown.org/protocol/1.0/unknown")
        assert entry is not None
        assert entry.fn is noop_handler

    def test_returns_none_without_catch_all(self) -> None:
        registry = HandlerRegistry()
        assert registry.lookup("https://unknown.org/anything") is None

    def test_duplicate_catch_all_raises(self) -> None:
        registry = HandlerRegistry()
        registry.register_catch_all(noop_handler)
        with pytest.raises(ValueError, match="catch-all handler already registered"):
            registry.register_catch_all(noop_handler)

    def test_protocols_includes_wildcard(self) -> None:
        registry = HandlerRegistry()
        registry.register("https://layr8.io/protocols/echo/1.0/request", noop_handler)
        registry.register_catch_all(noop_handler)
        protocols = registry.protocols()
        assert "*" in protocols
        assert "https://layr8.io/protocols/echo/1.0" in protocols

    def test_protocols_no_wildcard_without_catch_all(self) -> None:
        registry = HandlerRegistry()
        registry.register("https://layr8.io/protocols/echo/1.0/request", noop_handler)
        assert "*" not in registry.protocols()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_handler.py::TestCatchAll -v`
Expected: FAIL — `AttributeError: 'HandlerRegistry' object has no attribute 'register_catch_all'`

- [ ] **Step 3: Implement catch-all in HandlerRegistry**

In `src/layr8/handler.py`, update `HandlerRegistry`:

```python
class HandlerRegistry:
    """Thread-safe handler registry mapping message types to handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, HandlerEntry] = {}
        self._catch_all: HandlerEntry | None = None

    def register(
        self,
        msg_type: str,
        fn: HandlerFn,
    ) -> None:
        if msg_type in self._handlers:
            raise ValueError(
                f'handler already registered for message type "{msg_type}"'
            )
        self._handlers[msg_type] = HandlerEntry(fn=fn)

    def register_catch_all(self, fn: HandlerFn) -> None:
        if self._catch_all is not None:
            raise ValueError("catch-all handler already registered")
        self._catch_all = HandlerEntry(fn=fn)

    def lookup(self, msg_type: str) -> HandlerEntry | None:
        entry = self._handlers.get(msg_type)
        if entry is not None:
            return entry
        return self._catch_all

    def protocols(self) -> list[str]:
        """
        Return unique protocol base URIs derived from registered handler types.

        e.g. "https://layr8.io/protocols/echo/1.0/request"
             → "https://layr8.io/protocols/echo/1.0"

        Appends "*" if a catch-all handler is registered.
        """
        seen: set[str] = set()
        for msg_type in self._handlers:
            proto = _derive_protocol(msg_type)
            seen.add(proto)
        result = list(seen)
        if self._catch_all is not None:
            result.append("*")
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_handler.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```
git add src/layr8/handler.py tests/test_handler.py
git commit -m "Add catch-all handler and wildcard protocol"
```

---

### Task 4: Capability Negotiation in Channel

**Files:**
- Modify: `src/layr8/channel.py`

This task modifies the channel join logic. The channel is tested indirectly via `test_client.py` using the `MockPhoenixServer`. We'll add direct capability tests in Task 6 when we wire up the client. For now, we make the structural changes.

- [ ] **Step 1: Add `_reply_protocol` field and property to PhoenixChannel**

In `src/layr8/channel.py`, add to `__init__`:

```python
self._reply_protocol: bool = False
```

Add property after `assigned_did`:

```python
@property
def reply_protocol(self) -> bool:
    return self._reply_protocol
```

- [ ] **Step 2: Add `reply_protocol: True` to join payload**

In `_join()`, add to `join_payload`:

```python
join_payload: dict[str, Any] = {
    "payload_types": protocols,
    "reply_protocol": True,
    "did_spec": {
        ...
    },
}
```

- [ ] **Step 3: Parse capabilities from join reply**

In `_join()`, after the `if isinstance(response, dict) and response.get("did"):` block, add:

```python
capabilities = response.get("capabilities", []) if isinstance(response, dict) else []
self._reply_protocol = "reply_protocol/1" in capabilities
```

- [ ] **Step 4: Run full test suite**

Run: `pytest --tb=short`
Expected: all pass (existing mock server doesn't return capabilities, so `_reply_protocol` stays `False` — legacy mode)

- [ ] **Step 5: Commit**

```
git add src/layr8/channel.py
git commit -m "Add capability negotiation to channel join"
```

---

### Task 5: Client — Remove manual_ack, Add handle_all

**Files:**
- Modify: `src/layr8/client.py`
- Modify: `tests/test_client.py`

- [ ] **Step 1: Remove manual_ack from client.handle()**

In `src/layr8/client.py`, remove the `manual_ack` parameter from `handle()`:

```python
def handle(
    self,
    msg_type: str,
    fn: HandlerFn | None = None,
) -> Callable[[HandlerFn], HandlerFn] | None:
```

Remove `manual_ack=manual_ack` from both `self._registry.register()` calls inside `handle()`.

- [ ] **Step 2: Add handle_all() method**

Add after `handle()` in `src/layr8/client.py`:

```python
def handle_all(
    self,
    fn: HandlerFn | None = None,
) -> Callable[[HandlerFn], HandlerFn] | None:
    """
    Register a catch-all handler for any unhandled message type.

    Can be used as a decorator::

        @client.handle_all
        async def catch_all(msg: Message) -> Message | None:
            ...

    Or called directly::

        client.handle_all(my_fn)

    Must be called BEFORE ``connect()``.
    """
    if self._connected:
        raise AlreadyConnectedError()

    if fn is not None:
        self._registry.register_catch_all(fn)
        return None

    # Decorator mode
    def decorator(handler: HandlerFn) -> HandlerFn:
        self._registry.register_catch_all(handler)
        return handler

    return decorator
```

- [ ] **Step 3: Remove manual_ack tests from test_client.py**

In `tests/test_client.py`, there are no tests that directly use `manual_ack=True` on `client.handle()`, so no test deletions needed. However, remove the `manual_ack` import references if any exist.

- [ ] **Step 4: Remove ack-related code from _handle_inbound_message**

In `_handle_inbound_message`, remove the auto-ack block and the `_manual_ack` block (lines 271–282). We'll re-add the legacy-mode ack in Task 6. For now, just remove it:

Replace the block from `# Auto-ack before handler` through `msg._ack_fn = _manual_ack` with nothing — the handler just runs directly.

Also remove the `HandlerEntry` import reference to `manual_ack` usage anywhere in client.py if present.

- [ ] **Step 5: Run full test suite**

Run: `pytest --tb=short`
Expected: most pass. The `test_inbound_handler_dispatched` test checks for ack events — it will fail since we removed ack. We'll fix this in Task 6.

- [ ] **Step 6: Commit**

```
git add src/layr8/client.py
git commit -m "Remove manual_ack, add handle_all to client"
```

---

### Task 6: Client Dispatch — Reply Protocol and Legacy Mode

This is the core task. We rewrite `_handle_inbound_message` and `_run_handler` to branch on `reply_protocol`.

**Files:**
- Modify: `src/layr8/client.py`
- Modify: `tests/test_client.py`

- [ ] **Step 1: Write failing test — new mode, handler returns Message**

Add to `tests/test_client.py`:

```python
class TestReplyProtocol:
    """Tests for dispatch_reply protocol (new mode)."""

    async def test_handler_returns_message_sends_handled(
        self, mock_server: MockPhoenixServer
    ) -> None:
        """Handler returning Message → message sent + dispatch_reply 'handled'."""
        # Override to return capabilities with reply_protocol/1
        def handler(msg: dict[str, Any]) -> None:
            if msg["event"] == "phx_join":
                asyncio.ensure_future(
                    mock_server.send_to_client(
                        msg["ref"], msg["ref"], msg["topic"],
                        "phx_reply",
                        {
                            "status": "ok",
                            "response": {
                                "did": "did:web:node:test",
                                "capabilities": ["reply_protocol/1"],
                            },
                        },
                    )
                )
            elif msg["event"] == "message":
                if msg.get("ref"):
                    asyncio.ensure_future(
                        mock_server.send_to_client(
                            None, msg["ref"], msg["topic"],
                            "phx_reply", {"status": "ok", "response": {}},
                        )
                    )
            else:
                if msg.get("ref"):
                    asyncio.ensure_future(
                        mock_server.send_to_client(
                            None, msg["ref"], msg["topic"],
                            "phx_reply", {"status": "ok", "response": {}},
                        )
                    )

        mock_server.on_msg = handler

        client = Client(
            Config(node_url=ws_url(mock_server), api_key="test-key", agent_did="did:web:alice"),
            _discard_errors,
        )

        @client.handle("https://layr8.io/protocols/echo/1.0/request")
        async def echo(msg: Message) -> Message:
            body = msg.unmarshal_body()
            return Message(
                type="https://layr8.io/protocols/echo/1.0/response",
                body={"echo": body.get("message", "")},
            )

        await client.connect()

        await mock_server.send_to_client(
            None, None, "plugins:did:web:alice", "message",
            {
                "plaintext": {
                    "id": "req-1",
                    "type": "https://layr8.io/protocols/echo/1.0/request",
                    "from": "did:web:bob",
                    "to": ["did:web:alice"],
                    "body": {"message": "ping"},
                },
            },
        )

        await asyncio.sleep(0.5)
        received = mock_server.get_received()

        # Should have sent the response message
        msg_events = [
            r for r in received
            if r["event"] == "message"
            and isinstance(r["payload"], dict)
            and r["payload"].get("type") == "https://layr8.io/protocols/echo/1.0/response"
        ]
        assert len(msg_events) == 1

        # Should have sent dispatch_reply with status "handled"
        reply_events = [
            r for r in received
            if r["event"] == "dispatch_reply"
        ]
        assert len(reply_events) == 1
        assert reply_events[0]["payload"]["status"] == "handled"
        assert reply_events[0]["payload"]["message_id"] == "req-1"

        # Should NOT have sent ack
        ack_events = [r for r in received if r["event"] == "ack"]
        assert len(ack_events) == 0

        await client.close()

    async def test_handler_returns_none_sends_handled(
        self, mock_server: MockPhoenixServer
    ) -> None:
        """Handler returning None → dispatch_reply 'handled', no message sent."""
        def handler(msg: dict[str, Any]) -> None:
            if msg["event"] == "phx_join":
                asyncio.ensure_future(
                    mock_server.send_to_client(
                        msg["ref"], msg["ref"], msg["topic"],
                        "phx_reply",
                        {"status": "ok", "response": {"did": "did:web:node:test", "capabilities": ["reply_protocol/1"]}},
                    )
                )
            else:
                if msg.get("ref"):
                    asyncio.ensure_future(
                        mock_server.send_to_client(
                            None, msg["ref"], msg["topic"],
                            "phx_reply", {"status": "ok", "response": {}},
                        )
                    )

        mock_server.on_msg = handler

        client = Client(
            Config(node_url=ws_url(mock_server), api_key="test-key", agent_did="did:web:alice"),
            _discard_errors,
        )

        @client.handle("https://layr8.io/protocols/echo/1.0/request")
        async def echo(msg: Message) -> None:
            return None

        await client.connect()

        await mock_server.send_to_client(
            None, None, "plugins:did:web:alice", "message",
            {
                "plaintext": {
                    "id": "req-2",
                    "type": "https://layr8.io/protocols/echo/1.0/request",
                    "from": "did:web:bob",
                    "to": ["did:web:alice"],
                    "body": {"message": "ping"},
                },
            },
        )

        await asyncio.sleep(0.5)
        received = mock_server.get_received()

        reply_events = [r for r in received if r["event"] == "dispatch_reply"]
        assert len(reply_events) == 1
        assert reply_events[0]["payload"]["status"] == "handled"

        await client.close()

    async def test_handler_returns_pass_sends_pass(
        self, mock_server: MockPhoenixServer
    ) -> None:
        """Handler returning PASS → dispatch_reply 'pass'."""
        from layr8 import PASS

        def handler(msg: dict[str, Any]) -> None:
            if msg["event"] == "phx_join":
                asyncio.ensure_future(
                    mock_server.send_to_client(
                        msg["ref"], msg["ref"], msg["topic"],
                        "phx_reply",
                        {"status": "ok", "response": {"did": "did:web:node:test", "capabilities": ["reply_protocol/1"]}},
                    )
                )
            else:
                if msg.get("ref"):
                    asyncio.ensure_future(
                        mock_server.send_to_client(
                            None, msg["ref"], msg["topic"],
                            "phx_reply", {"status": "ok", "response": {}},
                        )
                    )

        mock_server.on_msg = handler

        client = Client(
            Config(node_url=ws_url(mock_server), api_key="test-key", agent_did="did:web:alice"),
            _discard_errors,
        )

        @client.handle("https://layr8.io/protocols/echo/1.0/request")
        async def echo(msg: Message):
            return PASS

        await client.connect()

        await mock_server.send_to_client(
            None, None, "plugins:did:web:alice", "message",
            {
                "plaintext": {
                    "id": "req-3",
                    "type": "https://layr8.io/protocols/echo/1.0/request",
                    "from": "did:web:bob",
                    "to": ["did:web:alice"],
                    "body": {},
                },
            },
        )

        await asyncio.sleep(0.5)
        received = mock_server.get_received()

        reply_events = [r for r in received if r["event"] == "dispatch_reply"]
        assert len(reply_events) == 1
        assert reply_events[0]["payload"]["status"] == "pass"

        await client.close()

    async def test_handler_raises_sends_error(
        self, mock_server: MockPhoenixServer
    ) -> None:
        """Handler raising exception → dispatch_reply 'error' + problem report."""
        def handler(msg: dict[str, Any]) -> None:
            if msg["event"] == "phx_join":
                asyncio.ensure_future(
                    mock_server.send_to_client(
                        msg["ref"], msg["ref"], msg["topic"],
                        "phx_reply",
                        {"status": "ok", "response": {"did": "did:web:node:test", "capabilities": ["reply_protocol/1"]}},
                    )
                )
            else:
                if msg.get("ref"):
                    asyncio.ensure_future(
                        mock_server.send_to_client(
                            None, msg["ref"], msg["topic"],
                            "phx_reply", {"status": "ok", "response": {}},
                        )
                    )

        mock_server.on_msg = handler

        client = Client(
            Config(node_url=ws_url(mock_server), api_key="test-key", agent_did="did:web:alice"),
            _discard_errors,
        )

        @client.handle("https://layr8.io/protocols/echo/1.0/request")
        async def echo(msg: Message) -> Message:
            raise ValueError("bad input")

        await client.connect()

        await mock_server.send_to_client(
            None, None, "plugins:did:web:alice", "message",
            {
                "plaintext": {
                    "id": "req-4",
                    "type": "https://layr8.io/protocols/echo/1.0/request",
                    "from": "did:web:bob",
                    "to": ["did:web:alice"],
                    "body": {},
                },
            },
        )

        await asyncio.sleep(0.5)
        received = mock_server.get_received()

        reply_events = [r for r in received if r["event"] == "dispatch_reply"]
        assert len(reply_events) == 1
        assert reply_events[0]["payload"]["status"] == "error"
        assert reply_events[0]["payload"]["code"] == "ValueError"
        assert reply_events[0]["payload"]["message"] == "bad input"

        # Problem report should also be sent
        reports = [
            r for r in received
            if r["event"] == "message"
            and isinstance(r["payload"], dict)
            and r["payload"].get("type") == "https://didcomm.org/report-problem/2.0/problem-report"
        ]
        assert len(reports) == 1

        await client.close()

    async def test_no_handler_sends_pass(
        self, mock_server: MockPhoenixServer
    ) -> None:
        """No handler and no catch-all → dispatch_reply 'pass'."""
        errors: list[SDKError] = []

        def handler(msg: dict[str, Any]) -> None:
            if msg["event"] == "phx_join":
                asyncio.ensure_future(
                    mock_server.send_to_client(
                        msg["ref"], msg["ref"], msg["topic"],
                        "phx_reply",
                        {"status": "ok", "response": {"did": "did:web:node:test", "capabilities": ["reply_protocol/1"]}},
                    )
                )
            else:
                if msg.get("ref"):
                    asyncio.ensure_future(
                        mock_server.send_to_client(
                            None, msg["ref"], msg["topic"],
                            "phx_reply", {"status": "ok", "response": {}},
                        )
                    )

        mock_server.on_msg = handler

        client = Client(
            Config(node_url=ws_url(mock_server), api_key="test-key", agent_did="did:web:alice"),
            errors.append,
        )

        @client.handle("https://layr8.io/protocols/echo/1.0/request")
        async def echo(msg: Message) -> None:
            return None

        await client.connect()

        # Send a message type that has no handler
        await mock_server.send_to_client(
            None, None, "plugins:did:web:alice", "message",
            {
                "plaintext": {
                    "id": "req-5",
                    "type": "https://unknown.org/protocol/1.0/unknown",
                    "from": "did:web:bob",
                    "to": ["did:web:alice"],
                    "body": {},
                },
            },
        )

        await asyncio.sleep(0.5)
        received = mock_server.get_received()

        reply_events = [r for r in received if r["event"] == "dispatch_reply"]
        assert len(reply_events) == 1
        assert reply_events[0]["payload"]["status"] == "pass"

        assert any(e.kind == ErrorKind.NO_HANDLER for e in errors)

        await client.close()

    async def test_catch_all_invoked(
        self, mock_server: MockPhoenixServer
    ) -> None:
        """Catch-all handler invoked when no specific handler matches."""
        received_msg: asyncio.Future[Message] = asyncio.get_running_loop().create_future()

        def handler(msg: dict[str, Any]) -> None:
            if msg["event"] == "phx_join":
                asyncio.ensure_future(
                    mock_server.send_to_client(
                        msg["ref"], msg["ref"], msg["topic"],
                        "phx_reply",
                        {"status": "ok", "response": {"did": "did:web:node:test", "capabilities": ["reply_protocol/1"]}},
                    )
                )
            else:
                if msg.get("ref"):
                    asyncio.ensure_future(
                        mock_server.send_to_client(
                            None, msg["ref"], msg["topic"],
                            "phx_reply", {"status": "ok", "response": {}},
                        )
                    )

        mock_server.on_msg = handler

        client = Client(
            Config(node_url=ws_url(mock_server), api_key="test-key", agent_did="did:web:alice"),
            _discard_errors,
        )

        @client.handle_all
        async def catch_all(msg: Message) -> None:
            if not received_msg.done():
                received_msg.set_result(msg)
            return None

        await client.connect()

        await mock_server.send_to_client(
            None, None, "plugins:did:web:alice", "message",
            {
                "plaintext": {
                    "id": "req-6",
                    "type": "https://any.org/protocol/1.0/anything",
                    "from": "did:web:bob",
                    "to": ["did:web:alice"],
                    "body": {"data": "test"},
                },
            },
        )

        msg = await asyncio.wait_for(received_msg, timeout=2)
        assert msg.type == "https://any.org/protocol/1.0/anything"

        await asyncio.sleep(0.3)
        received = mock_server.get_received()
        reply_events = [r for r in received if r["event"] == "dispatch_reply"]
        assert len(reply_events) == 1
        assert reply_events[0]["payload"]["status"] == "handled"

        await client.close()
```

- [ ] **Step 2: Write failing test — legacy mode preserves ack**

Append to `TestReplyProtocol` class:

```python
    async def test_legacy_mode_sends_ack(
        self, mock_server: MockPhoenixServer
    ) -> None:
        """Old server without capabilities → ack behavior preserved."""
        # Default mock_server handler does NOT return capabilities
        client = Client(
            Config(node_url=ws_url(mock_server), api_key="test-key", agent_did="did:web:alice"),
            _discard_errors,
        )

        @client.handle("https://layr8.io/protocols/echo/1.0/request")
        async def echo(msg: Message) -> None:
            return None

        await client.connect()

        mock_server.clear_received()
        await mock_server.send_to_client(
            None, None, "plugins:did:web:alice", "message",
            {
                "plaintext": {
                    "id": "req-legacy",
                    "type": "https://layr8.io/protocols/echo/1.0/request",
                    "from": "did:web:bob",
                    "to": ["did:web:alice"],
                    "body": {},
                },
            },
        )

        await asyncio.sleep(0.5)
        received = mock_server.get_received()

        ack_events = [r for r in received if r["event"] == "ack"]
        assert len(ack_events) == 1

        dispatch_events = [r for r in received if r["event"] == "dispatch_reply"]
        assert len(dispatch_events) == 0

        await client.close()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_client.py::TestReplyProtocol -v`
Expected: FAIL — new dispatch behavior not implemented yet

- [ ] **Step 4: Implement dispatch branching in client.py**

Rewrite `_handle_inbound_message` and `_run_handler` in `src/layr8/client.py`.

Import `PASS` and `_Pass` at top of file:

```python
from .sentinel import PASS, _Pass
```

Replace `_handle_inbound_message`:

```python
def _handle_inbound_message(self, payload: Any) -> None:
    """Called by the channel for each inbound 'message' event."""
    try:
        msg = parse_didcomm(payload)
    except Exception as exc:
        self._on_error(SDKError(
            kind=ErrorKind.PARSE_FAILURE,
            cause=exc,
            raw=payload,
        ))
        return

    # Check if this is a response to a pending Request (by thread ID)
    if msg.thread_id and msg.thread_id in self._pending:
        future = self._pending.pop(msg.thread_id)
        if not future.done():
            future.set_result(msg)
        return

    if self._channel and self._channel.reply_protocol:
        self._dispatch_new_mode(msg)
    else:
        self._dispatch_legacy_mode(msg)

def _dispatch_new_mode(self, msg: Message) -> None:
    """Dispatch using reply protocol — send dispatch_reply after handler."""
    entry = self._registry.lookup(msg.type)
    if not entry:
        self._on_error(SDKError(
            kind=ErrorKind.NO_HANDLER,
            message_id=msg.id,
            type=msg.type,
            from_did=msg.from_,
        ))
        asyncio.ensure_future(self._send_dispatch_reply(msg.id, "pass"))
        return

    asyncio.ensure_future(self._run_handler_new_mode(entry, msg))

def _dispatch_legacy_mode(self, msg: Message) -> None:
    """Dispatch using legacy ack protocol."""
    entry = self._registry.lookup(msg.type)
    if not entry:
        self._on_error(SDKError(
            kind=ErrorKind.NO_HANDLER,
            message_id=msg.id,
            type=msg.type,
            from_did=msg.from_,
        ))
        return

    # Auto-ack before handler
    if self._channel:
        task = asyncio.ensure_future(self._channel.send_ack([msg.id]))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    asyncio.ensure_future(self._run_handler(entry, msg))
```

Replace `_run_handler` and add `_run_handler_new_mode` and `_send_dispatch_reply`:

```python
async def _run_handler(self, entry: HandlerEntry, msg: Message) -> None:
    """Execute a handler and send back the response or problem report (legacy mode)."""
    try:
        resp = await entry.fn(msg)
    except Exception as exc:
        self._on_error(SDKError(
            kind=ErrorKind.HANDLER_EXCEPTION,
            message_id=msg.id,
            type=msg.type,
            from_did=msg.from_,
            cause=exc,
        ))
        try:
            await self._send_problem_report(msg, exc)
        except Exception:
            pass
        return

    if resp is not None and isinstance(resp, Message):
        self._fill_response(resp, msg)
        try:
            await self._send_message(resp)
        except Exception as exc:
            self._on_error(SDKError(
                kind=ErrorKind.TRANSPORT_WRITE,
                message_id=msg.id,
                type=msg.type,
                from_did=msg.from_,
                cause=exc,
            ))

async def _run_handler_new_mode(self, entry: HandlerEntry, msg: Message) -> None:
    """Execute a handler and send dispatch_reply (new mode)."""
    try:
        resp = await entry.fn(msg)
    except Exception as exc:
        self._on_error(SDKError(
            kind=ErrorKind.HANDLER_EXCEPTION,
            message_id=msg.id,
            type=msg.type,
            from_did=msg.from_,
            cause=exc,
        ))
        try:
            await self._send_problem_report(msg, exc)
        except Exception:
            pass
        await self._send_dispatch_reply(
            msg.id, "error",
            code=type(exc).__name__,
            message=str(exc),
        )
        return

    if isinstance(resp, _Pass):
        await self._send_dispatch_reply(msg.id, "pass")
        return

    if isinstance(resp, Message):
        self._fill_response(resp, msg)
        try:
            await self._send_message(resp)
        except Exception as exc:
            self._on_error(SDKError(
                kind=ErrorKind.TRANSPORT_WRITE,
                message_id=msg.id,
                type=msg.type,
                from_did=msg.from_,
                cause=exc,
            ))

    await self._send_dispatch_reply(msg.id, "handled")

def _fill_response(self, resp: Message, original: Message) -> None:
    """Auto-fill response fields from the original message."""
    if not resp.from_:
        resp.from_ = self._agent_did
    if not resp.to and original.from_:
        resp.to = [original.from_]
    if not resp.thread_id:
        resp.thread_id = original.thread_id or original.id
    if not resp.id:
        resp.id = generate_id()

async def _send_dispatch_reply(
    self,
    message_id: str,
    status: str,
    *,
    code: str = "",
    message: str = "",
) -> None:
    """Send a dispatch_reply event to the cloud node."""
    if not self._channel:
        return
    payload: dict[str, Any] = {
        "message_id": message_id,
        "status": status,
    }
    if code:
        payload["code"] = code
    if message:
        payload["message"] = message
    try:
        await self._channel.send_fire_and_forget("dispatch_reply", payload)
    except Exception:
        pass
```

- [ ] **Step 5: Update existing tests that check for ack**

In `tests/test_client.py`, update `test_inbound_handler_dispatched`: the default mock server returns no capabilities, so it's legacy mode. The ack assertion should still pass. No changes needed.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_client.py -v`
Expected: all pass

- [ ] **Step 7: Run full test suite**

Run: `pytest --tb=short`
Expected: all pass

- [ ] **Step 8: Commit**

```
git add src/layr8/client.py tests/test_client.py
git commit -m "Add dispatch_reply protocol with legacy fallback"
```

---

### Task 7: Clean Up Message — Remove ack

**Files:**
- Modify: `src/layr8/message.py`
- Modify: `tests/test_message.py`

- [ ] **Step 1: Remove TestAck from test_message.py**

Delete the entire `TestAck` class (lines 111–120) from `tests/test_message.py`.

- [ ] **Step 2: Remove _ack_fn and ack() from Message**

In `src/layr8/message.py`:

Remove the `_ack_fn` field from `Message`:
```python
_ack_fn: Callable[..., Any] | None = field(default=None, repr=False)
```

Remove the `ack()` method:
```python
def ack(self) -> None:
    """Manually acknowledge this message (only with manual_ack=True)."""
    if self._ack_fn is not None:
        self._ack_fn(self.id)
```

Remove the `Callable` import from `typing` if no longer used (check — `Callable` is still imported from `collections.abc` in handler.py but not needed in message.py after removing `_ack_fn`). Remove `Callable` from the `from typing import Any, Callable` line, leaving just `from typing import Any`.

- [ ] **Step 3: Run full test suite**

Run: `pytest --tb=short`
Expected: all pass

- [ ] **Step 4: Commit**

```
git add src/layr8/message.py tests/test_message.py
git commit -m "Remove ack from Message"
```

---

### Task 8: Update README and Examples

**Files:**
- Modify: `README.md`
- Modify: `examples/durable_handler.py` (if it uses `manual_ack`)

- [ ] **Step 1: Check examples for manual_ack usage**

Read `examples/durable_handler.py` and any other examples to identify `manual_ack` or `msg.ack()` usage.

- [ ] **Step 2: Update README**

In `README.md`:

1. Update the **Handlers** section to mention `handle_all`:

```markdown
### Handlers

Register handlers with `@client.handle()` before calling `connect()`. A handler receives a `Message` and returns `Message` (sends response), `None` (no response), `PASS` (decline to handle), or raises an exception (sends a DIDComm problem report).
```

2. Add a **Wildcard Handler** subsection after Handlers:

```markdown
### Wildcard Handler

Register a catch-all for any message type not matched by a specific handler:

\```python
from layr8 import PASS

@client.handle_all
async def catch_all(msg: Message) -> Message | None:
    if msg.type.startswith("https://myorg.com/"):
        return Message(type=msg.type + "/ack", body={"ok": True})
    return PASS  # decline — let the cloud-node route elsewhere
\```
```

3. Remove the **Durable Handlers** section entirely (the `manual_ack` / `msg.ack()` pattern).

4. Update the handler return description in the Architecture section if needed.

- [ ] **Step 3: Update durable_handler.py example**

Rewrite the example to use the natural dispatch_reply pattern — the handler just processes and returns `None`. No `manual_ack` or `msg.ack()`.

- [ ] **Step 4: Run full test suite one final time**

Run: `pytest --tb=short`
Expected: all pass

- [ ] **Step 5: Commit**

```
git add README.md examples/durable_handler.py
git commit -m "Update docs for reply protocol and handle_all"
```
