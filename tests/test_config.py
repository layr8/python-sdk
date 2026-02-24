"""Tests for layr8.config."""

from __future__ import annotations

import os

import pytest

from layr8.config import Config, resolve_config
from layr8.errors import Layr8Error


class TestResolveConfig:
    def test_uses_provided_values(self) -> None:
        cfg = resolve_config(Config(
            node_url="ws://localhost:4000",
            api_key="my-key",
            agent_did="did:web:test",
        ))
        assert cfg.node_url == "ws://localhost:4000"
        assert cfg.api_key == "my-key"
        assert cfg.agent_did == "did:web:test"

    def test_falls_back_to_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LAYR8_NODE_URL", "ws://env-node:4000")
        monkeypatch.setenv("LAYR8_API_KEY", "env-key")
        monkeypatch.setenv("LAYR8_AGENT_DID", "did:web:env")

        cfg = resolve_config(Config())
        assert cfg.node_url == "ws://env-node:4000"
        assert cfg.api_key == "env-key"
        assert cfg.agent_did == "did:web:env"

    def test_raises_when_node_url_missing(self) -> None:
        with pytest.raises(Layr8Error, match="node_url is required"):
            resolve_config(Config(api_key="key"))

    def test_raises_when_api_key_missing(self) -> None:
        with pytest.raises(Layr8Error, match="api_key is required"):
            resolve_config(Config(node_url="ws://localhost:4000"))

    def test_normalizes_https_to_wss(self) -> None:
        cfg = resolve_config(Config(
            node_url="https://mynode.layr8.cloud/plugin_socket/websocket",
            api_key="key",
        ))
        assert cfg.node_url == "wss://mynode.layr8.cloud/plugin_socket/websocket"

    def test_normalizes_http_to_ws(self) -> None:
        cfg = resolve_config(Config(
            node_url="http://localhost:4000/plugin_socket/websocket",
            api_key="key",
        ))
        assert cfg.node_url == "ws://localhost:4000/plugin_socket/websocket"

    def test_allows_empty_agent_did(self) -> None:
        cfg = resolve_config(Config(node_url="ws://localhost:4000", api_key="key"))
        assert cfg.agent_did == ""
