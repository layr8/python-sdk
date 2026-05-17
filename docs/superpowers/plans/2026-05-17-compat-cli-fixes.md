# Compat CLI Fixes + Release Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three gaps preventing the Python SDK compat image from working with the compat-suite orchestrator: receiver ready signal, agent DID passthrough, and release pipeline.

**Architecture:** Modify scenario types, all four scenario `run_receiver`/`run_sender` functions, and the CLI adapter. Add a GitHub Actions release workflow. All changes are on the existing `feat/compat-integration` branch.

**Tech Stack:** Python 3.12, layr8 SDK, pytest, pytest-asyncio, GitHub Actions

---

## File Structure

```
compat/
├── scenarios/
│   ├── types.py             # MODIFY: add agent_did to ScenarioContext
│   ├── echo.py              # MODIFY: add on_ready, pass agent_did
│   ├── pass_scenario.py     # MODIFY: add on_ready, pass agent_did
│   ├── wildcard.py          # MODIFY: add on_ready, pass agent_did
│   └── disconnected.py      # MODIFY: pass agent_did to sender
├── bin/
│   └── compat.py            # MODIFY: emit ready signal, pass --did
├── tests/
│   ├── test_types.py        # MODIFY: test agent_did field
│   ├── test_echo.py         # MODIFY: MockPhoenixServer DID support, on_ready test
│   ├── test_pass.py         # MODIFY: pass on_ready
│   ├── test_wildcard.py     # MODIFY: pass on_ready
│   └── test_disconnected.py # no change (no receiver)
.github/
└── workflows/
    └── release.yaml         # CREATE: release pipeline
```

## SDK API Quick Reference

```python
from layr8 import Client, Config, Message, PASS, log_errors

# Config accepts agent_did — if empty, node assigns one on join
client = Client(Config(node_url="ws://...", api_key="...", agent_did="did:web:..."), log_errors())

# client.did returns the DID (from config or assigned by node) after connect
async with client:
    print(client.did)  # available here
```

---

## Task 1: Add agent_did to ScenarioContext and update tests

**Files:**
- Modify: `compat/scenarios/types.py:9-16`
- Modify: `compat/tests/test_types.py:31-53`

- [ ] **Step 1: Write the failing test**

Add a new test to `compat/tests/test_types.py` inside the `TestContexts` class:

```python
    def test_scenario_context_agent_did_default(self) -> None:
        ctx = ScenarioContext(
            node_url="ws://localhost:4000/plugin_socket/websocket",
            api_key="test-key",
            test_id="test-123",
            timeout=10.0,
        )
        assert ctx.agent_did == ""

    def test_scenario_context_agent_did_explicit(self) -> None:
        ctx = ScenarioContext(
            node_url="ws://localhost:4000/plugin_socket/websocket",
            api_key="test-key",
            test_id="test-123",
            timeout=10.0,
            agent_did="did:web:node:agent-1",
        )
        assert ctx.agent_did == "did:web:node:agent-1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd compat && python -m pytest tests/test_types.py::TestContexts::test_scenario_context_agent_did_default -v`
Expected: FAIL — `TypeError: ScenarioContext.__init__() got an unexpected keyword argument 'agent_did'`

- [ ] **Step 3: Add agent_did to ScenarioContext**

In `compat/scenarios/types.py`, add `agent_did` field to `ScenarioContext`:

```python
@dataclass
class ScenarioContext:
    """Context provided to both sender and receiver scenario functions."""

    node_url: str
    api_key: str
    test_id: str
    timeout: float
    agent_did: str = ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd compat && python -m pytest tests/test_types.py -v`
Expected: PASS (all 7 tests — 5 existing + 2 new)

- [ ] **Step 5: Commit**

```bash
git add compat/scenarios/types.py compat/tests/test_types.py
git commit -m "Add agent_did to ScenarioContext"
```

---

## Task 2: Update MockPhoenixServer to support explicit DIDs

**Files:**
- Modify: `compat/tests/test_echo.py:16-94`

The mock currently auto-assigns `did:web:node:agent-{counter}` on every join. When a scenario passes `agent_did` to `Config`, the SDK joins with topic `plugins:{did}`. The mock needs to extract the DID from the topic and use it if present, falling back to auto-assign if the topic is `plugins:` (empty DID).

- [ ] **Step 1: Write the failing test**

Add a new test class to `compat/tests/test_echo.py`:

```python
class TestMockPhoenixServerDID:
    async def test_explicit_did_from_join_topic(self, mock_server: MockPhoenixServer) -> None:
        """When a client joins with an explicit DID in the topic, the mock uses it."""
        explicit_did = "did:web:node:explicit-test"
        ctx = ScenarioContext(
            node_url=mock_server.ws_url,
            api_key="test-key",
            test_id="test-did-1",
            timeout=5.0,
            agent_did=explicit_did,
        )
        ready_dids: list[str] = []

        async def capture_ready(did: str) -> None:
            ready_dids.append(did)

        receiver_task = asyncio.create_task(
            run_receiver(ctx, on_ready=capture_ready)
        )
        await asyncio.sleep(0.3)
        receiver_task.cancel()
        try:
            await receiver_task
        except asyncio.CancelledError:
            pass

        assert ready_dids == [explicit_did]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd compat && python -m pytest tests/test_echo.py::TestMockPhoenixServerDID -v`
Expected: FAIL — `run_receiver()` does not accept `on_ready` yet (this test depends on Tasks 2 and 3 both being complete)

Note: This test will stay failing until Task 3 adds `on_ready` to `run_receiver`. Implement the mock change now, and the test will pass after Task 3.

- [ ] **Step 3: Update MockPhoenixServer to extract DID from topic**

In `compat/tests/test_echo.py`, modify the `_handler` method. Replace the DID assignment logic (lines 35-38):

```python
    async def _handler(self, ws: websockets.asyncio.server.ServerConnection) -> None:
        self._connections.append(ws)
        conn_id = id(ws)

        try:
            async for raw in ws:
                arr = json.loads(raw)
                join_ref, ref, topic, event, payload = arr

                if event == "phx_join":
                    # Extract DID from topic "plugins:{did}" — if empty, auto-assign
                    topic_did = topic.removeprefix("plugins:")
                    if topic_did:
                        assigned_did = topic_did
                    else:
                        self._did_counter += 1
                        assigned_did = f"did:web:node:agent-{self._did_counter}"
                    self._assigned_dids[conn_id] = assigned_did

                    await ws.send(json.dumps([
                        ref, ref, topic, "phx_reply",
                        {
                            "status": "ok",
                            "response": {
                                "did": assigned_did,
                                "capabilities": ["reply_protocol/1"],
                            },
                        },
                    ]))
                elif event == "message":
```

The rest of the handler stays the same. Also remove the `self._did_counter += 1` and `assigned_did = ...` lines that were at the top of the method (lines 36-38 in current code), since assignment now happens inside the `phx_join` block.

- [ ] **Step 4: Run existing tests to verify nothing broke**

Run: `cd compat && python -m pytest tests/test_echo.py::TestEchoScenario -v`
Expected: PASS (existing test still works — auto-assign path still used since echo scenario doesn't pass `agent_did` yet)

- [ ] **Step 5: Commit**

```bash
git add compat/tests/test_echo.py
git commit -m "Update MockPhoenixServer to support explicit DIDs"
```

---

## Task 3: Add on_ready callback to echo scenario

**Files:**
- Modify: `compat/scenarios/echo.py`
- Modify: `compat/tests/test_echo.py`

- [ ] **Step 1: Write the failing test**

Add a test to `compat/tests/test_echo.py` in `TestEchoScenario`:

```python
    async def test_on_ready_called_with_did(self, mock_server: MockPhoenixServer) -> None:
        """run_receiver calls on_ready with the client DID after connecting."""
        receiver_ctx = ScenarioContext(
            node_url=mock_server.ws_url,
            api_key="test-key",
            test_id="test-ready-1",
            timeout=5.0,
        )
        ready_dids: list[str] = []

        receiver_task = asyncio.create_task(
            run_receiver(receiver_ctx, on_ready=lambda did: ready_dids.append(did))
        )
        await asyncio.sleep(0.3)

        assert len(ready_dids) == 1
        assert ready_dids[0].startswith("did:web:")

        receiver_task.cancel()
        try:
            await receiver_task
        except asyncio.CancelledError:
            pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd compat && python -m pytest tests/test_echo.py::TestEchoScenario::test_on_ready_called_with_did -v`
Expected: FAIL — `run_receiver() got an unexpected keyword argument 'on_ready'`

- [ ] **Step 3: Update echo scenario**

Replace `compat/scenarios/echo.py` with:

```python
"""Echo scenario — basic request/response messaging."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from layr8 import Client, Config, Message, log_errors

from .types import ScenarioContext, SenderContext, ScenarioResult, elapsed_ms

ECHO_TYPE = "https://layr8.test/echo/1.0/request"
ECHO_RESPONSE_TYPE = "https://layr8.test/echo/1.0/response"


async def run_receiver(
    ctx: ScenarioContext,
    on_ready: Callable[[str], None] | None = None,
) -> None:
    """Connect and register echo handler. Blocks until cancelled."""
    client = Client(
        Config(node_url=ctx.node_url, api_key=ctx.api_key, agent_did=ctx.agent_did),
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
        if on_ready:
            on_ready(client.did)
        await asyncio.Event().wait()


async def run_sender(ctx: SenderContext) -> ScenarioResult:
    """Send an echo request and verify the response."""
    client = Client(
        Config(node_url=ctx.node_url, api_key=ctx.api_key, agent_did=ctx.agent_did),
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
```

Changes from current: added `on_ready` param, `Callable` import, `agent_did=ctx.agent_did` in both `Config()` calls, `on_ready(client.did)` call after connect.

- [ ] **Step 4: Update existing echo test to pass on_ready**

In `compat/tests/test_echo.py`, update `test_echo_passes` to pass `on_ready` to receiver:

```python
        # Start receiver in background
        receiver_task = asyncio.create_task(run_receiver(receiver_ctx, on_ready=lambda did: None))
```

- [ ] **Step 5: Run all echo tests**

Run: `cd compat && python -m pytest tests/test_echo.py -v`
Expected: PASS (all tests including the new `test_on_ready_called_with_did` and `TestMockPhoenixServerDID`)

- [ ] **Step 6: Commit**

```bash
git add compat/scenarios/echo.py compat/tests/test_echo.py
git commit -m "Add on_ready callback and agent_did to echo scenario"
```

---

## Task 4: Add on_ready callback to pass scenario

**Files:**
- Modify: `compat/scenarios/pass_scenario.py`
- Modify: `compat/tests/test_pass.py`

- [ ] **Step 1: Update pass_scenario.py**

Replace `compat/scenarios/pass_scenario.py` with:

```python
"""Pass scenario — handler returns PASS sentinel.

Tests that when a handler returns PASS, the cloud-node treats
the message as unhandled (no response is sent back to the sender).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from layr8 import Client, Config, Message, PASS, log_errors

from .types import ScenarioContext, SenderContext, ScenarioResult, elapsed_ms

PASS_TYPE = "https://layr8.test/pass/1.0/request"


async def run_receiver(
    ctx: ScenarioContext,
    on_ready: Callable[[str], None] | None = None,
) -> None:
    """Connect and register a handler that returns PASS. Blocks until cancelled."""
    client = Client(
        Config(node_url=ctx.node_url, api_key=ctx.api_key, agent_did=ctx.agent_did),
        log_errors(),
    )

    @client.handle(PASS_TYPE)
    async def handler(msg: Message) -> Message | None:
        return PASS  # type: ignore[return-value]

    async with client:
        if on_ready:
            on_ready(client.did)
        await asyncio.Event().wait()


async def run_sender(ctx: SenderContext) -> ScenarioResult:
    """Send a message and verify no response comes back (timeout expected)."""
    client = Client(
        Config(node_url=ctx.node_url, api_key=ctx.api_key, agent_did=ctx.agent_did),
        log_errors(),
    )
    start = time.monotonic()

    try:
        async with client:
            try:
                await client.request(
                    Message(
                        type=PASS_TYPE,
                        to=[ctx.receiver_did],
                        body={"test_id": ctx.test_id},
                    ),
                    timeout=ctx.timeout,
                )
                return ScenarioResult(
                    "fail", "pass", elapsed_ms(start),
                    error="expected timeout but got response",
                )
            except asyncio.TimeoutError:
                return ScenarioResult("pass", "pass", elapsed_ms(start))
    except Exception as e:
        return ScenarioResult("fail", "pass", elapsed_ms(start), error=str(e))
```

- [ ] **Step 2: Update test to pass on_ready**

In `compat/tests/test_pass.py`, update the receiver task line:

```python
        receiver_task = asyncio.create_task(run_receiver(receiver_ctx, on_ready=lambda did: None))
```

- [ ] **Step 3: Run tests**

Run: `cd compat && python -m pytest tests/test_pass.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add compat/scenarios/pass_scenario.py compat/tests/test_pass.py
git commit -m "Add on_ready callback and agent_did to pass scenario"
```

---

## Task 5: Add on_ready callback to wildcard scenario

**Files:**
- Modify: `compat/scenarios/wildcard.py`
- Modify: `compat/tests/test_wildcard.py`

- [ ] **Step 1: Update wildcard.py**

Replace `compat/scenarios/wildcard.py` with:

```python
"""Wildcard scenario — catch-all handler via handle_all.

Tests that a receiver using handle_all can respond to any message type.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from layr8 import Client, Config, Message, log_errors

from .types import ScenarioContext, SenderContext, ScenarioResult, elapsed_ms

WILDCARD_REQUEST_TYPE = "https://layr8.test/wildcard/1.0/request"
WILDCARD_RESPONSE_TYPE = "https://layr8.test/wildcard/1.0/response"


async def run_receiver(
    ctx: ScenarioContext,
    on_ready: Callable[[str], None] | None = None,
) -> None:
    """Connect with only a catch-all handler. Blocks until cancelled."""
    client = Client(
        Config(node_url=ctx.node_url, api_key=ctx.api_key, agent_did=ctx.agent_did),
        log_errors(),
    )

    @client.handle_all
    async def catch_all(msg: Message) -> Message:
        body = msg.unmarshal_body()
        return Message(
            type=WILDCARD_RESPONSE_TYPE,
            body={"received": body, "from": client.did},
        )

    async with client:
        if on_ready:
            on_ready(client.did)
        await asyncio.Event().wait()


async def run_sender(ctx: SenderContext) -> ScenarioResult:
    """Send a message with an arbitrary type and verify catch-all responds."""
    client = Client(
        Config(node_url=ctx.node_url, api_key=ctx.api_key, agent_did=ctx.agent_did),
        log_errors(),
    )
    start = time.monotonic()

    try:
        async with client:
            resp = await client.request(
                Message(
                    type=WILDCARD_REQUEST_TYPE,
                    to=[ctx.receiver_did],
                    body={"ping": ctx.test_id},
                ),
                timeout=ctx.timeout,
            )
            body = resp.unmarshal_body()
            received = body.get("received", {})
            if isinstance(received, dict) and received.get("ping") == ctx.test_id:
                return ScenarioResult("pass", "wildcard", elapsed_ms(start))
            return ScenarioResult(
                "fail", "wildcard", elapsed_ms(start),
                error=f"unexpected response: {received!r}",
            )
    except Exception as e:
        return ScenarioResult("fail", "wildcard", elapsed_ms(start), error=str(e))
```

- [ ] **Step 2: Update test to pass on_ready**

In `compat/tests/test_wildcard.py`, update the receiver task line:

```python
        receiver_task = asyncio.create_task(run_receiver(receiver_ctx, on_ready=lambda did: None))
```

- [ ] **Step 3: Run tests**

Run: `cd compat && python -m pytest tests/test_wildcard.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add compat/scenarios/wildcard.py compat/tests/test_wildcard.py
git commit -m "Add on_ready callback and agent_did to wildcard scenario"
```

---

## Task 6: Add agent_did to disconnected scenario sender

**Files:**
- Modify: `compat/scenarios/disconnected.py`

The disconnected scenario has no receiver (it raises `NotImplementedError`). Only the sender needs `agent_did` added to `Config`.

- [ ] **Step 1: Update disconnected.py**

Replace `compat/scenarios/disconnected.py` with:

```python
"""Disconnected scenario — message to an offline agent.

Tests that sending a message to a DID with no connected agent
results in a clean timeout, not a crash or hang.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from layr8 import Client, Config, Message, log_errors

from .types import ScenarioContext, SenderContext, ScenarioResult, elapsed_ms

DISCONNECTED_TYPE = "https://layr8.test/disconnected/1.0/request"


async def run_receiver(
    ctx: ScenarioContext,
    on_ready: Callable[[str], None] | None = None,
) -> None:
    """No receiver for this scenario — the point is that nobody is listening."""
    raise NotImplementedError("disconnected scenario has no receiver")


async def run_sender(ctx: SenderContext) -> ScenarioResult:
    """Send to a non-existent DID and verify clean timeout."""
    client = Client(
        Config(node_url=ctx.node_url, api_key=ctx.api_key, agent_did=ctx.agent_did),
        log_errors(),
    )
    start = time.monotonic()

    try:
        async with client:
            try:
                await client.request(
                    Message(
                        type=DISCONNECTED_TYPE,
                        to=[ctx.receiver_did],
                        body={"test_id": ctx.test_id},
                    ),
                    timeout=ctx.timeout,
                )
                return ScenarioResult(
                    "fail", "disconnected", elapsed_ms(start),
                    error="expected timeout but got response",
                )
            except asyncio.TimeoutError:
                return ScenarioResult("pass", "disconnected", elapsed_ms(start))
    except Exception as e:
        return ScenarioResult(
            "fail", "disconnected", elapsed_ms(start), error=str(e),
        )
```

Changes: added `Callable` import, `on_ready` param to `run_receiver` signature (for interface consistency), `agent_did=ctx.agent_did` in sender `Config`.

- [ ] **Step 2: Run tests**

Run: `cd compat && python -m pytest tests/test_disconnected.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add compat/scenarios/disconnected.py
git commit -m "Add agent_did to disconnected scenario"
```

---

## Task 7: Update CLI adapter for ready signal and --did passthrough

**Files:**
- Modify: `compat/bin/compat.py`
- Modify: `compat/tests/test_cli.py`

The ready signal is already tested in-process via `test_on_ready_called_with_did` (Task 3). The CLI test verifies argument parsing works correctly with `--did` in receiver mode. No subprocess-level ready signal test needed — the in-process test + the CLI argument test together cover the full path.

- [ ] **Step 1: Verify no test changes needed in test_cli.py**

The existing `TestListScenarios` tests still pass. No new CLI tests are required — the ready signal behavior is covered by the in-process scenario tests from Tasks 3-5.

- [ ] **Step 2: Update CLI adapter**

Replace `compat/bin/compat.py` with:

```python
"""Layer 2 CLI adapter for the compat-suite orchestrator.

Usage:
    python -m bin.compat --list-scenarios
    python -m bin.compat --mode sender --scenario echo --node ws://... --did did:web:...
    python -m bin.compat --mode receiver --scenario echo --node ws://... --did did:web:...
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

SCENARIOS_DIR = Path(__file__).parent.parent / "scenarios"


def list_scenarios() -> list[str]:
    """Discover available scenario names from the scenarios/ package."""
    names: list[str] = []
    for f in sorted(SCENARIOS_DIR.glob("*.py")):
        name = f.stem
        if name.startswith("_") or name == "types":
            continue
        display = name.removesuffix("_scenario")
        names.append(display)
    return names


def _module_name(scenario: str) -> str:
    """Map a scenario display name to its Python module name."""
    module_path = SCENARIOS_DIR / f"{scenario}.py"
    if module_path.exists():
        return f"scenarios.{scenario}"
    module_path = SCENARIOS_DIR / f"{scenario}_scenario.py"
    if module_path.exists():
        return f"scenarios.{scenario}_scenario"
    raise ValueError(f"unknown scenario: {scenario}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Layr8 compat-suite CLI adapter")
    parser.add_argument("--mode", choices=["sender", "receiver"])
    parser.add_argument("--scenario")
    parser.add_argument("--node", help="Cloud-node WebSocket URL")
    parser.add_argument("--did", help="Agent DID (receiver) or receiver DID (sender)")
    parser.add_argument("--api-key", default=os.environ.get("LAYR8_API_KEY", "test-key"))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--test-id", default="cli")
    parser.add_argument("--list-scenarios", action="store_true")
    args = parser.parse_args()

    if args.list_scenarios:
        print(json.dumps(list_scenarios()))
        return

    if not args.mode or not args.scenario:
        parser.error("--mode and --scenario are required")

    module_name = _module_name(args.scenario)
    mod = importlib.import_module(module_name)

    from scenarios.types import ScenarioContext, SenderContext

    if args.mode == "receiver":
        ctx = ScenarioContext(
            node_url=args.node,
            api_key=args.api_key,
            test_id=args.test_id,
            timeout=args.timeout,
            agent_did=args.did or "",
        )

        def on_ready(did: str) -> None:
            print(json.dumps({"status": "ready", "did": did}), flush=True)

        asyncio.run(mod.run_receiver(ctx, on_ready=on_ready))
    elif args.mode == "sender":
        if not args.did:
            parser.error("--did is required in sender mode")
        ctx = SenderContext(
            node_url=args.node,
            api_key=args.api_key,
            test_id=args.test_id,
            timeout=args.timeout,
            receiver_did=args.did,
        )
        result = asyncio.run(mod.run_sender(ctx))
        print(json.dumps({
            "status": result.status,
            "scenario": result.scenario,
            "duration_ms": result.duration_ms,
            "error": result.error,
        }))
        if result.status != "pass":
            sys.exit(1)


if __name__ == "__main__":
    main()
```

Changes: receiver mode now passes `agent_did=args.did`, creates `on_ready` callback that prints JSON with flush, passes it to `run_receiver`. The `--did` help text updated to clarify dual purpose.

- [ ] **Step 3: Run all tests**

Run: `cd compat && python -m pytest tests/ -v --ignore=tests/conftest.py`
Expected: PASS (all tests)

- [ ] **Step 4: Commit**

```bash
git add compat/bin/compat.py
git commit -m "Emit ready signal and pass --did in CLI adapter"
```

---

## Task 8: Release workflow

**Files:**
- Create: `.github/workflows/release.yaml`

- [ ] **Step 1: Create the release workflow**

Create `.github/workflows/release.yaml`:

```yaml
name: Release

on:
  release:
    types: [published]

permissions: {}

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -e ".[dev]"

      - name: Test
        run: pytest -v

  compat-unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install SDK and compat deps
        run: |
          pip install -e .
          pip install -e "compat/[test]"

      - name: Compat unit tests
        run: cd compat && pytest tests/ -v --ignore=tests/conftest.py -k "not layer1"

  validate-version:
    runs-on: ubuntu-latest
    outputs:
      version: ${{ steps.check.outputs.version }}
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Validate tag matches pyproject.toml
        id: check
        run: |
          TAG_VERSION="${GITHUB_REF_NAME#v}"
          TOML_VERSION=$(python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
          if [ "$TAG_VERSION" != "$TOML_VERSION" ]; then
            echo "::error::Tag $TAG_VERSION != pyproject.toml $TOML_VERSION"
            exit 1
          fi
          echo "version=$TAG_VERSION" >> "$GITHUB_OUTPUT"

  publish-pypi:
    needs: [test, compat-unit, validate-version]
    runs-on: ubuntu-latest
    permissions:
      id-token: write
    environment: pypi
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Build
        run: pip install build && python -m build

      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1

  publish-compat-image:
    needs: [publish-pypi, validate-version]
    runs-on: ubuntu-latest
    permissions:
      packages: write
    steps:
      - uses: actions/checkout@v4

      - name: Log in to ghcr.io
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push compat image
        run: |
          VERSION=${{ needs.validate-version.outputs.version }}
          IMAGE=ghcr.io/layr8/python-sdk/compat
          docker build \
            --build-arg SDK_VERSION=$VERSION \
            -t $IMAGE:$VERSION \
            -f compat/Dockerfile compat/
          docker push $IMAGE:$VERSION

  compat-gate:
    needs: [publish-compat-image, validate-version]
    uses: layr8/compat-suite/.github/workflows/gate.yml@main
    with:
      sdk: python
      version: ${{ needs.validate-version.outputs.version }}
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/release.yaml
git commit -m "Add release workflow with PyPI and compat gate"
```

---

## Task 9: Update README and create CONTEXT.md

**Files:**
- Modify: `README.md`
- Create: `CONTEXT.md`

- [ ] **Step 1: Add compat section to README.md**

Add the following section after the "Development" section (before "## Architecture") in `README.md`:

```markdown
## Compatibility Testing

The `compat/` directory contains the compatibility test suite — scenarios that validate the SDK against real cloud-node versions and cross-language interoperability with other Layr8 SDKs.

### Architecture

The compat suite uses a hexagonal architecture:

- **Scenarios** (`compat/scenarios/`) — pure async Python, no framework dependencies. Each scenario exposes `run_receiver(ctx, on_ready=)` and `run_sender(ctx)` functions.
- **Layer 1** (`compat/tests/`) — pytest + testcontainers adapter. Spins up real cloud-node Docker containers and runs scenarios against them.
- **Layer 2** (`compat/bin/compat.py`) — CLI adapter for the compat-suite orchestrator. Implements the standard interface: `--mode sender|receiver --scenario <name> --node <url> --did <did>`.

### Running Locally

```bash
# Unit tests (mock server, no Docker needed)
cd compat && pytest tests/ -v --ignore=tests/conftest.py

# Layer 1 integration tests (requires Docker)
cd compat && pip install -e ".[test]" && pytest tests/ -v
```

### Adding a Scenario

1. Create `compat/scenarios/<name>.py` with `run_receiver` and `run_sender`
2. Create `compat/tests/test_<name>.py` with unit tests using `MockPhoenixServer`
3. The CLI auto-discovers scenarios from the `scenarios/` directory
```

- [ ] **Step 2: Create CONTEXT.md**

Create `CONTEXT.md` at the repo root:

```markdown
# Context — Layr8 Python SDK

## Ubiquitous Language

| Term | Definition |
|---|---|
| **Agent** | A software process that connects to a cloud-node and exchanges DIDComm v2 messages. An agent is identified by a DID. |
| **Cloud-node** | A Layr8 infrastructure component that routes DIDComm messages between agents. Agents connect via WebSocket using the Phoenix Channel V2 protocol. |
| **DID** | Decentralized Identifier — a globally unique agent identity (e.g., `did:web:myorg:my-agent`). May be configured explicitly or assigned by the cloud-node on connect. |
| **Handler** | An async function registered for a specific DIDComm message type. Receives a `Message`, returns a response `Message`, `None`, or `PASS`. |
| **PASS** | A sentinel value returned by a handler to decline a message — signals to the cloud-node that this agent does not handle this message type. |
| **Scenario** | A compat-suite test case. Each scenario is a pair of async functions (`run_receiver`, `run_sender`) that exercise a specific SDK behavior against a cloud-node. |
| **Compat image** | A Docker image (`ghcr.io/layr8/python-sdk/compat:{version}`) that packages the scenario code and CLI adapter. Consumed by the compat-suite orchestrator. |
| **Ready signal** | A JSON line (`{"status":"ready","did":"..."}`) printed to stdout by a receiver process after connecting and registering handlers. The compat-suite orchestrator waits for this before launching the sender. |
| **Layer 1** | Pytest + testcontainers adapter — runs scenarios against real cloud-node Docker containers. |
| **Layer 2** | CLI adapter — implements the compat-suite orchestrator's interface (`--mode`, `--scenario`, `--node`, `--did`). |
| **Compat-suite orchestrator** | A separate repo (`layr8/compat-suite`) that pairs SDK compat images across languages and cloud-node versions, runs test matrices, and produces compatibility reports. |
```

- [ ] **Step 3: Commit**

```bash
git add README.md CONTEXT.md
git commit -m "Add compat docs to README and create CONTEXT.md"
```

---

## Execution Order

1. Task 1 (agent_did on ScenarioContext)
2. Task 2 (MockPhoenixServer explicit DID support)
3. Task 3 (echo on_ready + agent_did)
4. Task 4 (pass on_ready + agent_did)
5. Task 5 (wildcard on_ready + agent_did)
6. Task 6 (disconnected agent_did)
7. Task 7 (CLI adapter ready signal + --did)
8. Task 8 (release workflow)
9. Task 9 (README + CONTEXT.md)
