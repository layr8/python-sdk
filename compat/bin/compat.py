"""Layer 2 CLI adapter for the compat-suite orchestrator.

Usage:
    python -m bin.compat --list-scenarios
    python -m bin.compat --mode sender --scenario echo --node ws://... --did did:web:...
    python -m bin.compat --mode receiver --scenario echo --node ws://...
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

SCENARIOS_DIR = Path(__file__).parent.parent / "scenarios"


def list_scenarios() -> list[str]:
    """Discover available scenario names from the scenarios/ package."""
    names: list[str] = []
    for f in sorted(SCENARIOS_DIR.glob("*.py")):
        name = f.stem
        if name.startswith("_") or name == "types":
            continue
        # Normalize: pass_scenario -> pass
        display = name.removesuffix("_scenario")
        names.append(display)
    return names


def _module_name(scenario: str) -> str:
    """Map a scenario display name to its Python module name."""
    module_path = SCENARIOS_DIR / f"{scenario}.py"
    if module_path.exists():
        return f"scenarios.{scenario}"
    module_path = SCENARIOS_DIR / f"{scenario}_scenario.py"
    if module_path.exists():
        return f"scenarios.{scenario}_scenario"
    raise ValueError(f"unknown scenario: {scenario}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Layr8 compat-suite CLI adapter")
    parser.add_argument("--mode", choices=["sender", "receiver"])
    parser.add_argument("--scenario")
    parser.add_argument("--node", help="Cloud-node WebSocket URL")
    parser.add_argument("--did", help="Receiver DID (sender mode only)")
    parser.add_argument("--api-key", default=os.environ.get("LAYR8_API_KEY", "test-key"))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--test-id", default="cli")
    parser.add_argument("--list-scenarios", action="store_true")
    args = parser.parse_args()

    if args.list_scenarios:
        print(json.dumps(list_scenarios()))
        return

    if not args.mode or not args.scenario:
        parser.error("--mode and --scenario are required")

    module_name = _module_name(args.scenario)
    mod = importlib.import_module(module_name)

    from scenarios.types import ScenarioContext, SenderContext

    if args.mode == "receiver":
        ctx = ScenarioContext(
            node_url=args.node,
            api_key=args.api_key,
            test_id=args.test_id,
            timeout=args.timeout,
        )
        asyncio.run(mod.run_receiver(ctx))
    elif args.mode == "sender":
        if not args.did:
            parser.error("--did is required in sender mode")
        ctx = SenderContext(
            node_url=args.node,
            api_key=args.api_key,
            test_id=args.test_id,
            timeout=args.timeout,
            receiver_did=args.did,
        )
        result = asyncio.run(mod.run_sender(ctx))
        print(json.dumps({
            "status": result.status,
            "scenario": result.scenario,
            "duration_ms": result.duration_ms,
            "error": result.error,
        }))
        if result.status != "pass":
            sys.exit(1)


if __name__ == "__main__":
    main()
