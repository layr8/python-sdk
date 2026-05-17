# PRD: Python SDK — Compat Integration (Layer 1 + Layer 2 Image)

## Problem

The Python SDK (`layr8/python-sdk`) has no compat test infrastructure.
The compat-suite currently has a stub that prints
`{"status":"fail","scenario":"unimplemented"}`. All compat scenarios
need to be implemented from scratch.

## Goal

Add a `compat/` directory to the Python SDK repo implementing the same
hexagonal architecture: scenario core logic, Layer 1 tests (pytest +
testcontainers), and Layer 2 CLI adapter + Dockerfile. CI publishes
`ghcr.io/layr8/python-sdk/compat:{version}` on release.

## Target Structure

```
python-sdk/
└── compat/
    ├── cloud_nodes.json         # cloud-node version declaration
    ├── scenarios/               # core — pure domain logic
    │   ├── __init__.py
    │   ├── types.py             #   ScenarioContext, SenderContext, Result
    │   ├── echo.py              #   run_sender(ctx), run_receiver(ctx)
    │   ├── pass_scenario.py     #   ('pass' is a Python keyword)
    │   ├── wildcard.py
    │   └── disconnected.py
    ├── tests/                   # adapter: Layer 1 (pytest)
    │   ├── conftest.py          #   testcontainers fixtures
    │   ├── test_echo.py         #   parameterized over cloud-node versions
    │   ├── test_pass.py
    │   ├── test_wildcard.py
    │   └── test_disconnected.py
    ├── bin/                     # adapter: Layer 2 (CLI)
    │   └── compat.py            #   --mode/--scenario/--node/--did/--list-scenarios
    ├── Dockerfile
    ├── pyproject.toml           # compat-specific deps (pytest, testcontainers)
    └── cloud_nodes.json
```

## Scenario Port (Python)

```python
# scenarios/types.py
from dataclasses import dataclass
from typing import Callable, Optional
from layr8_sdk import Client

@dataclass
class ScenarioContext:
    create_client: Callable[[Optional[str]], Client]
    test_id: str
    timeout: float  # seconds — Python uses asyncio timeouts

@dataclass
class SenderContext(ScenarioContext):
    receiver_did: str

@dataclass
class ScenarioResult:
    status: str       # "pass" | "fail"
    scenario: str
    duration_ms: int
    error: Optional[str] = None
```

```python
# scenarios/echo.py
from .types import ScenarioContext, SenderContext, ScenarioResult

ECHO_TYPE = "https://layr8.test/echo/1.0/request"
ECHO_RESPONSE_TYPE = "https://layr8.test/echo/1.0/response"

async def run_receiver(ctx: ScenarioContext) -> None:
    client = ctx.create_client(None)
    @client.handle(ECHO_TYPE)
    async def handler(msg):
        return {"type": ECHO_RESPONSE_TYPE, "body": {"echo": msg.body, "from": client.did}}
    await client.connect(timeout=ctx.timeout)

async def run_sender(ctx: SenderContext) -> ScenarioResult:
    client = ctx.create_client(None)
    await client.connect(timeout=ctx.timeout)
    start = time.time()
    try:
        response = await client.request(
            type=ECHO_TYPE,
            to=[ctx.receiver_did],
            body={"ping": ctx.test_id},
            timeout=ctx.timeout,
        )
        if response.body.get("echo", {}).get("ping") == ctx.test_id:
            return ScenarioResult("pass", "echo", elapsed_ms(start))
        return ScenarioResult("fail", "echo", elapsed_ms(start))
    except Exception as e:
        return ScenarioResult("fail", "echo", elapsed_ms(start), error=str(e))
    finally:
        await client.close()
```

## Layer 1 (pytest + testcontainers)

Uses `testcontainers` Python package:

```python
# tests/conftest.py
import pytest
from testcontainers.core.container import DockerContainer

@pytest.fixture(scope="session")
def cloud_nodes():
    """Start one cloud-node container per declared version."""
    # Read cloud_nodes.json, resolve against manifest
    # Start containers, yield list of (version, url)
    # Tear down after all tests complete

@pytest.fixture(params=cloud_node_versions)
def node_url(request, cloud_nodes):
    """Parameterize each test over cloud-node versions."""
    return cloud_nodes[request.param]
```

```python
# tests/test_echo.py
import pytest
from compat.scenarios.echo import run_sender, run_receiver
from compat.scenarios.types import ScenarioContext, SenderContext

async def test_echo(node_url):
    # Construct contexts with factory wired to node_url
    # Start receiver, run sender, assert pass
    result = await run_sender(sender_ctx)
    assert result.status == "pass"
```

## Layer 2 (CLI)

```python
# bin/compat.py
"""Layer 2 CLI adapter for compat-suite orchestrator."""
import argparse, asyncio, json, importlib, os

SCENARIOS_DIR = os.path.join(os.path.dirname(__file__), "..", "scenarios")

def list_scenarios():
    """Discover available scenarios from the scenarios/ package."""
    # List .py files in scenarios/, exclude __init__.py and types.py
    # Return scenario names

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["sender", "receiver"])
    parser.add_argument("--scenario")
    parser.add_argument("--node")
    parser.add_argument("--did")
    parser.add_argument("--list-scenarios", action="store_true")
    args = parser.parse_args()

    if args.list_scenarios:
        print(json.dumps(list_scenarios()))
        return

    # Import scenario module, construct context, run
```

## Dockerfile

```dockerfile
FROM python:3.12-alpine
WORKDIR /app
ARG SDK_VERSION
RUN pip install layr8-sdk==${SDK_VERSION}
COPY compat/scenarios/ ./scenarios/
COPY compat/bin/ ./bin/
LABEL org.opencontainers.image.source=https://github.com/layr8/python-sdk
ENTRYPOINT ["python", "-m", "bin.compat"]
```

**Image**: `ghcr.io/layr8/python-sdk/compat:{version}`

## Cloud-Node Declaration

```json
{
  "image": "ghcr.io/layr-8/cloud-node",
  "min": "4.13.0",
  "exclude": {
    "4.14.0": "Accepts reply_protocol from join but doesn't advertise capability"
  }
}
```

## CI Workflow

```yaml
jobs:
  build:
    # pytest, mypy/pyright

  compat-layer1:
    needs: build
    steps:
      - run: cd compat && pytest tests/

  publish-sdk:
    # pypa/gh-action-pypi-publish (OIDC trusted publisher)
    needs: [build, compat-layer1]

  publish-compat-image:
    needs: publish-sdk
    steps:
      - run: |
          docker build --build-arg SDK_VERSION=$VERSION \
            -t ghcr.io/layr8/python-sdk/compat:$VERSION \
            -f compat/Dockerfile .
          docker push ghcr.io/layr8/python-sdk/compat:$VERSION

  compat-gate:
    needs: publish-compat-image
    uses: layr8/compat-suite/.github/workflows/gate.yml@main
    with:
      sdk: python
      version: ${{ needs.build.outputs.version }}
```

## README Update

Update the Python SDK README.md to document:
- The `compat/` directory structure and hexagonal architecture
- How to run Layer 1 locally (`cd compat && pytest tests/`)
- Cloud-node version declaration (`compat/cloud_nodes.json`)
- CI ordering: build → Layer 1 → publish PyPI → compat image → Layer 2
- That Layer 2 gate failures are informational (SDK already published)
- How to add a new scenario
- How to add support for a new cloud-node version

## Implementation Steps

1. Create `compat/pyproject.toml` with deps (layr8-sdk, pytest,
   testcontainers, pytest-asyncio)
2. Create `compat/scenarios/types.py` with context and result types
3. Implement scenarios (echo first, then pass, wildcard, disconnected)
4. Create `compat/tests/conftest.py` with testcontainers fixtures
5. Create `compat/tests/test_*.py` — one per scenario
6. Create `compat/bin/compat.py` with CLI adapter
7. Create `compat/Dockerfile`
8. Create `compat/cloud_nodes.json`
9. Add CI workflow steps
10. Verify Layer 1 passes, build and test compat image locally
