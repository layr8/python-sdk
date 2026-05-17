"""Unit tests for the wildcard scenario logic."""

from __future__ import annotations

import asyncio
import pytest

from scenarios.wildcard import run_receiver, run_sender
from scenarios.types import ScenarioContext, SenderContext
from tests.test_echo import MockPhoenixServer


@pytest.fixture
async def mock_server():
    server = MockPhoenixServer()
    await server.start()
    yield server
    await server.close()


class TestWildcardScenario:
    async def test_wildcard_passes(self, mock_server: MockPhoenixServer) -> None:
        """Catch-all handler responds, sender gets pass."""
        receiver_ctx = ScenarioContext(
            node_url=mock_server.ws_url,
            api_key="test-key",
            test_id="test-wildcard-1",
            timeout=5.0,
        )

        receiver_task = asyncio.create_task(run_receiver(receiver_ctx, on_ready=lambda did: None))
        await asyncio.sleep(0.3)

        sender_ctx = SenderContext(
            node_url=mock_server.ws_url,
            api_key="test-key",
            test_id="test-wildcard-1",
            timeout=5.0,
            receiver_did="did:web:node:agent-1",
        )

        result = await run_sender(sender_ctx)
        assert result.status == "pass"
        assert result.scenario == "wildcard"

        receiver_task.cancel()
        try:
            await receiver_task
        except asyncio.CancelledError:
            pass