"""Tests for the Layer 2 CLI adapter."""

from __future__ import annotations

import json
import subprocess
import sys


class TestListScenarios:
    def test_list_scenarios_returns_json(self) -> None:
        """--list-scenarios returns a JSON array of scenario names."""
        result = subprocess.run(
            [sys.executable, "-m", "bin.compat", "--list-scenarios"],
            capture_output=True,
            text=True,
            cwd=str(__import__("pathlib").Path(__file__).parent.parent),
        )
        assert result.returncode == 0
        scenarios = json.loads(result.stdout)
        assert isinstance(scenarios, list)
        assert "echo" in scenarios
        assert "pass" in scenarios
        assert "wildcard" in scenarios
        assert "disconnected" in scenarios

    def test_list_does_not_include_types_or_init(self) -> None:
        """Scenario discovery excludes __init__.py and types.py."""
        result = subprocess.run(
            [sys.executable, "-m", "bin.compat", "--list-scenarios"],
            capture_output=True,
            text=True,
            cwd=str(__import__("pathlib").Path(__file__).parent.parent),
        )
        scenarios = json.loads(result.stdout)
        assert "__init__" not in scenarios
        assert "types" not in scenarios