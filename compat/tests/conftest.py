"""Pytest fixtures for compat Layer 1 integration tests.

Provides cloud-node containers via testcontainers. Tests that use the
`node_url` fixture will be parameterized over all declared cloud-node
versions from cloud_nodes.json.

Requires Docker to be available. Tests are skipped if Docker is not running.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs
    HAS_TESTCONTAINERS = True
except ImportError:
    HAS_TESTCONTAINERS = False

CLOUD_NODES_PATH = Path(__file__).parent.parent / "cloud_nodes.json"


def _load_cloud_node_versions() -> list[str]:
    """Load declared cloud-node versions from cloud_nodes.json."""
    if not CLOUD_NODES_PATH.exists():
        return []
    with open(CLOUD_NODES_PATH) as f:
        data = json.load(f)
    # For now, just use the min version. A real implementation would
    # query the container registry for all tags >= min and filter excludes.
    return [data["min"]]


CLOUD_NODE_VERSIONS = _load_cloud_node_versions()


@pytest.fixture(scope="session")
def cloud_node_containers():
    """Start one cloud-node container per declared version.

    Yields a dict mapping version string to WebSocket URL.
    Containers are stopped after the entire test session.
    """
    if not HAS_TESTCONTAINERS:
        pytest.skip("testcontainers not installed")

    if not CLOUD_NODE_VERSIONS:
        pytest.skip("no cloud-node versions declared")

    with open(CLOUD_NODES_PATH) as f:
        config = json.load(f)

    image_base = config["image"]
    excludes = config.get("exclude", {})
    containers: dict[str, tuple[DockerContainer, str]] = {}

    for version in CLOUD_NODE_VERSIONS:
        if version in excludes:
            continue

        image = f"{image_base}:{version}"
        container = DockerContainer(image)
        container.with_exposed_ports(4000)
        container.start()
        wait_for_logs(container, "Running", timeout=30)

        host = container.get_container_host_ip()
        port = container.get_exposed_port(4000)
        ws_url = f"ws://{host}:{port}/plugin_socket/websocket"
        containers[version] = (container, ws_url)

    yield {v: url for v, (_, url) in containers.items()}

    for _, (container, _) in containers.items():
        container.stop()


@pytest.fixture(params=CLOUD_NODE_VERSIONS or ["mock"])
def node_url(request, cloud_node_containers):
    """Parameterize each test over cloud-node versions."""
    version = request.param
    if version == "mock":
        pytest.skip("no cloud-node versions available")
    return cloud_node_containers[version]