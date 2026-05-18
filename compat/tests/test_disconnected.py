"""Unit tests for the disconnected scenario logic."""

from __future__ import annotations

import asyncio

import pytest

from scenarios.disconnected import run_receiver, run_sender
from scenarios.types import ScenarioContext, SenderContext
from tests.test_echo import MockPhoenixServer


@pytest.fixture
async def mock_server():
    server = MockPhoenixServer()
    await server.start()
    yield server
    await server.close()


class TestDisconnectedScenario:
    async def test_disconnected_passes(self, mock_server: MockPhoenixServer) -> None:
        """Sending to a non-existent DID times out gracefully → pass."""
        sender_ctx = SenderContext(
            node_url=mock_server.ws_url,
            api_key="test-key",
            test_id="test-disconnected-1",
            timeout=1.0,
            receiver_did="did:web:nobody:nonexistent",
        )

        result = await run_sender(sender_ctx)
        assert result.status == "pass"
        assert result.scenario == "disconnected"

    async def test_receiver_connects_and_signals_ready(
        self, mock_server: MockPhoenixServer
    ) -> None:
        """Receiver connects, emits ready with DID, and waits."""
        ctx = ScenarioContext(
            node_url=mock_server.ws_url,
            api_key="test-key",
            test_id="test-disconnected-recv",
            timeout=5.0,
            agent_did="did:web:node:disconnected-recv",
        )

        ready_did: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        def on_ready(did: str) -> None:
            if not ready_did.done():
                ready_did.set_result(did)

        task = asyncio.create_task(run_receiver(ctx, on_ready=on_ready))

        did = await asyncio.wait_for(ready_did, timeout=3.0)
        assert did  # non-empty DID

        # Clean up — cancel the blocking receiver
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass