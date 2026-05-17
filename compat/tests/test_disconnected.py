"""Unit tests for the disconnected scenario logic."""

from __future__ import annotations

import pytest

from scenarios.disconnected import run_sender
from scenarios.types import SenderContext
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