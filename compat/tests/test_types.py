"""Tests for compat scenario types."""

import time

from scenarios.types import ScenarioContext, SenderContext, ScenarioResult, elapsed_ms


class TestScenarioResult:
    def test_pass_result(self) -> None:
        r = ScenarioResult(status="pass", scenario="echo", duration_ms=42)
        assert r.status == "pass"
        assert r.scenario == "echo"
        assert r.duration_ms == 42
        assert r.error is None

    def test_fail_result_with_error(self) -> None:
        r = ScenarioResult(status="fail", scenario="echo", duration_ms=10, error="timeout")
        assert r.status == "fail"
        assert r.error == "timeout"


class TestElapsedMs:
    def test_returns_positive_int(self) -> None:
        start = time.monotonic()
        time.sleep(0.01)
        ms = elapsed_ms(start)
        assert isinstance(ms, int)
        assert ms >= 10


class TestContexts:
    def test_scenario_context_fields(self) -> None:
        ctx = ScenarioContext(
            node_url="ws://localhost:4000/plugin_socket/websocket",
            api_key="test-key",
            test_id="test-123",
            timeout=10.0,
        )
        assert ctx.node_url == "ws://localhost:4000/plugin_socket/websocket"
        assert ctx.api_key == "test-key"
        assert ctx.test_id == "test-123"
        assert ctx.timeout == 10.0

    def test_sender_context_extends_scenario_context(self) -> None:
        ctx = SenderContext(
            node_url="ws://localhost:4000/plugin_socket/websocket",
            api_key="test-key",
            test_id="test-123",
            timeout=10.0,
            receiver_did="did:web:receiver",
        )
        assert ctx.receiver_did == "did:web:receiver"
        assert ctx.node_url == "ws://localhost:4000/plugin_socket/websocket"
