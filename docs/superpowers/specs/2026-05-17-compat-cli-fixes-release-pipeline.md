# Compat CLI Fixes + Release Pipeline

**Date:** 2026-05-17
**Status:** Draft
**Branch:** feat/compat-integration (extends existing work)

## Problem

The compat infrastructure built in the initial PR has three gaps that
prevent end-to-end use with the compat-suite orchestrator:

1. **No receiver ready signal.** The orchestrator watches receiver
   stdout for `{"status":"ready","did":"..."}` before launching the
   sender. Our receiver blocks forever without printing anything.

2. **No agent DID passthrough.** The orchestrator passes `--did` to
   both sender and receiver. The receiver must connect with that
   specific DID so the sender can address it. Our CLI ignores `--did`
   in receiver mode, and scenarios construct `Config()` without
   `agent_did`.

3. **No release pipeline.** There is no workflow to publish the SDK to
   PyPI, build the compat image, push it to ghcr.io, or call the
   compat-suite gate.

## Design

### 1. Receiver ready signal

Add an `on_ready` callback to every `run_receiver` function:

```python
async def run_receiver(
    ctx: ScenarioContext,
    on_ready: Callable[[str], None] | None = None,
) -> None:
```

After connecting and registering handlers (inside `async with
client:`), the scenario calls `on_ready(client.did)` before blocking
on `await asyncio.Event().wait()`.

The CLI adapter passes a lambda that prints the ready signal:

```python
on_ready=lambda did: print(
    json.dumps({"status": "ready", "did": did}), flush=True
)
```

This preserves the hexagonal boundary: scenarios are unaware of
stdout, the CLI adapter decides what to do with the signal.

The disconnected scenario has no receiver — its `run_receiver` raises
`NotImplementedError`. No change needed there.

### 2. Agent DID passthrough

Add `agent_did: str = ""` to `ScenarioContext`:

```python
@dataclass
class ScenarioContext:
    node_url: str
    api_key: str
    test_id: str
    timeout: float
    agent_did: str = ""
```

Every scenario passes it through to `Config`:

```python
client = Client(
    Config(
        node_url=ctx.node_url,
        api_key=ctx.api_key,
        agent_did=ctx.agent_did,
    ),
    log_errors(),
)
```

The CLI adapter populates `agent_did` from `--did` in both sender and
receiver modes. `SenderContext` continues to have `receiver_did` for
the sender to know who to send to — these are two different DIDs.

In receiver mode, `--did` is the agent's own DID. In sender mode,
`--did` is the receiver's DID (mapped to `receiver_did`), and the
sender gets its own DID assigned by the node.

### 3. Compat image

Built from `python-sdk/compat/Dockerfile`. Published to
`ghcr.io/layr8/python-sdk/compat:{version}`.

The Dockerfile installs the SDK from PyPI at a pinned version:

```dockerfile
ARG SDK_VERSION
RUN pip install --no-cache-dir layr8==${SDK_VERSION}
```

The existing Dockerfile already has this structure. The OCI source
label points to the python-sdk repo for ghcr.io linking.

### 4. Release pipeline

Triggered by creating a GitHub Release (which creates a git tag).

#### Workflow: `.github/workflows/release.yaml`

```
on:
  release:
    types: [published]
```

**Jobs:**

1. **test** — run SDK unit tests (same as existing CI job)
2. **compat-unit** — run compat scenario unit tests (same as existing)
3. **validate-version** — extract version from git tag, compare to
   `pyproject.toml` version, fail if they don't match
4. **publish-pypi** — `pypa/gh-action-pypi-publish` with OIDC trusted
   publisher. Needs `id-token: write` permission. Depends on test,
   compat-unit, validate-version.
5. **publish-compat-image** — build compat Dockerfile with
   `--build-arg SDK_VERSION={version}`, push to
   `ghcr.io/layr8/python-sdk/compat:{version}`. Needs `packages:
   write` permission. Depends on publish-pypi (SDK must be on PyPI
   before the image can install it).
6. **compat-gate** — call
   `layr8/compat-suite/.github/workflows/gate.yml` with `sdk: python`
   and `version: {version}`. Informational — the SDK is already
   published, so gate failure doesn't block the release, but it
   reports whether the release is compatible. Depends on
   publish-compat-image.

#### Version management

The version lives in `pyproject.toml` as a hardcoded string. It is
bumped as part of the release PR. The CI validates the tag matches:

```bash
TAG_VERSION="${GITHUB_REF_NAME#v}"
TOML_VERSION=$(python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
if [ "$TAG_VERSION" != "$TOML_VERSION" ]; then
  echo "Tag $TAG_VERSION != pyproject.toml $TOML_VERSION"
  exit 1
fi
```

### 5. Existing CI (unchanged)

The existing `ci.yaml` continues to run `test` and `compat-unit` on
push/PR to main. No changes needed.

## Files changed

### Modified
- `compat/scenarios/types.py` — add `agent_did` field to `ScenarioContext`
- `compat/scenarios/echo.py` — add `on_ready` param, pass `agent_did` to Config
- `compat/scenarios/pass_scenario.py` — same
- `compat/scenarios/wildcard.py` — same
- `compat/scenarios/disconnected.py` — no receiver change (already raises NotImplementedError), pass `agent_did` to sender Config
- `compat/bin/compat.py` — emit ready signal in receiver mode, pass `--did` through as `agent_did`
- `compat/tests/test_echo.py` — update for `on_ready` param
- `compat/tests/test_pass.py` — same
- `compat/tests/test_wildcard.py` — same
- `compat/tests/test_types.py` — test `agent_did` field

### New
- `.github/workflows/release.yaml` — release pipeline

## Out of scope

- Main-branch compat image builds (add later if needed)
- Layer 1 integration test files (fixtures exist, tests not yet written)
- compat-suite changes (matrix.json already has the correct Python image path)
