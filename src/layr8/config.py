"""Configuration for the Layr8 SDK."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import Layr8Error


@dataclass
class Config:
    """
    Configuration for a Layr8 client.

    All fields fall back to environment variables if empty:
      - node_url  -> LAYR8_NODE_URL (required)
      - api_key   -> LAYR8_API_KEY (required)
      - agent_did -> LAYR8_AGENT_DID (optional)
    """

    node_url: str = ""
    api_key: str = ""
    agent_did: str = ""


@dataclass(frozen=True)
class ResolvedConfig:
    """Resolved configuration with required fields guaranteed present."""

    node_url: str
    api_key: str
    agent_did: str


def resolve_config(cfg: Config) -> ResolvedConfig:
    """Fill empty fields from environment variables and validate required fields."""
    node_url = cfg.node_url or os.environ.get("LAYR8_NODE_URL", "")
    api_key = cfg.api_key or os.environ.get("LAYR8_API_KEY", "")
    agent_did = cfg.agent_did or os.environ.get("LAYR8_AGENT_DID", "")

    if not node_url:
        raise Layr8Error(
            "node_url is required (set in Config or LAYR8_NODE_URL env)"
        )

    # Normalize HTTP(S) URLs to WebSocket scheme.
    # In production, the /plugin_socket endpoint serves WebSocket over HTTPS.
    if node_url.startswith("https://"):
        node_url = "wss://" + node_url.removeprefix("https://")
    elif node_url.startswith("http://"):
        node_url = "ws://" + node_url.removeprefix("http://")

    if not api_key:
        raise Layr8Error(
            "api_key is required (set in Config or LAYR8_API_KEY env)"
        )

    return ResolvedConfig(node_url=node_url, api_key=api_key, agent_did=agent_did)
