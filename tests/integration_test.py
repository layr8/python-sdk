#!/usr/bin/env python3
"""Integration test for the Layr8 Python SDK against live cloud-nodes.

Tests all key SDK functions against live cloud-nodes:
  - Client / Config (explicit configuration)
  - handle (register handlers, decorator style)
  - connect / close / did
  - send (server-acked)
  - request (cross-node request/response)
  - W3C Verifiable Credentials (sign, verify, store, list, get)
  - W3C Verifiable Presentations (sign, verify)

Prerequisites:
  - Two nodes running in local Tilt env (alice-test, bob-test)
  - Traefik exposing *.localhost (k3d maps host :80/:443 to Traefik)

Usage:
    python tests/integration_test.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any

from layr8 import Client, Config, Message, RESTError, log_errors
from layr8.credentials import Credential

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALICE_NODE_URL = "ws://alice-test.localhost/plugin_socket/websocket"
ALICE_API_KEY = "alice_abcd1234_testkeyalicetestkeyali24"

BOB_NODE_URL = "ws://bob-test.localhost/plugin_socket/websocket"
BOB_API_KEY = "bob_efgh5678_testkeybobbtestkeybobt24"

ECHO_BASE = "https://layr8.io/protocols/echo-test/1.0"
ECHO_REQUEST = ECHO_BASE + "/request"
ECHO_RESPONSE = ECHO_BASE + "/response"

# ---------------------------------------------------------------------------
# ANSI color codes (matching Go SDK)
# ---------------------------------------------------------------------------

COLOR_RESET = "\033[0m"
COLOR_GREEN = "\033[32m"
COLOR_RED = "\033[31m"
COLOR_YELLOW = "\033[33m"
COLOR_CYAN = "\033[36m"
COLOR_BOLD = "\033[1m"
COLOR_DIM = "\033[2m"

# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

passed = 0
failed = 0
skipped = 0


def section(name: str) -> None:
    """Print a section header."""
    print(f"\n{COLOR_BOLD}{COLOR_CYAN}-- {name} --{COLOR_RESET}\n")


def pass_(name: str) -> None:
    """Record and print a passing test."""
    global passed
    print(f"    {COLOR_BOLD}{COLOR_GREEN} PASS {COLOR_RESET} {name}")
    passed += 1


def fail_(name: str, reason: str) -> None:
    """Record and print a failing test."""
    global failed
    print(f"    {COLOR_BOLD}{COLOR_RED} FAIL {COLOR_RESET} {name}")
    print(f"         {COLOR_DIM}{reason}{COLOR_RESET}")
    failed += 1


def skip_(name: str, reason: str) -> None:
    """Record and print a skipped test."""
    global skipped
    print(f"    {COLOR_BOLD}{COLOR_YELLOW} SKIP {COLOR_RESET} {name}")
    print(f"         {COLOR_DIM}{reason}{COLOR_RESET}")
    skipped += 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    test_id = str(int(time.time() * 1000))
    alice_did = f"did:web:alice-test.localhost:sdk-test-{test_id}"
    bob_did = f"did:web:bob-test.localhost:sdk-test-{test_id}"

    print(f"\n{COLOR_BOLD}{COLOR_CYAN}=== Layr8 Python SDK -- Integration Test ==={COLOR_RESET}")
    print(f"{COLOR_DIM}Test ID:   {test_id}{COLOR_RESET}")
    print(f"{COLOR_DIM}Alice DID: {alice_did}{COLOR_RESET}")
    print(f"{COLOR_DIM}Bob DID:   {bob_did}{COLOR_RESET}")

    alice: Client | None = None
    bob: Client | None = None
    signed_cred: str = ""

    try:
        # ==================================================================
        section("Connection & Identity")
        # ==================================================================

        # Test 1: Connect Alice with echo handler
        print("  [1] Connect Alice with echo handler")

        alice = Client(
            Config(
                node_url=ALICE_NODE_URL,
                api_key=ALICE_API_KEY,
                agent_did=alice_did,
            ),
            on_error=log_errors(),
        )

        @alice.handle(ECHO_REQUEST)
        async def alice_echo(msg: Message) -> Message:
            body = msg.unmarshal_body()
            logging.info("  [alice echo] from %s: %r", msg.from_, body.get("message"))
            return Message(
                type=ECHO_RESPONSE,
                body={
                    "echo": body.get("message", ""),
                    "timestamp": time.time_ns(),
                },
            )

        try:
            await alice.connect()
            pass_("Alice connected with echo handler")
        except Exception as exc:
            fail_("Alice connect", str(exc))
            return

        # Test 2: DID() returns expected value
        print("  [2] DID returns expected value")

        if alice.did == alice_did:
            pass_(f"alice.did = {alice.did}")
        else:
            fail_("DID mismatch", f"got {alice.did!r}, want {alice_did!r}")

        # Test 3: Connect Bob
        print("  [3] Connect Bob")

        bob = Client(
            Config(
                node_url=BOB_NODE_URL,
                api_key=BOB_API_KEY,
                agent_did=bob_did,
            ),
            on_error=log_errors(),
        )

        @bob.handle(ECHO_REQUEST)
        async def bob_echo(msg: Message) -> None:
            return None

        try:
            await bob.connect()
            pass_("Bob connected")
        except Exception as exc:
            fail_("Bob connect", str(exc))
            return

        # ==================================================================
        section("Cross-Node Messaging")
        # ==================================================================

        # Test 4: Request/Response echo (Bob -> Alice)
        print("  [4] Request/Response -- echo protocol (Bob -> Alice)")

        try:
            resp = await bob.request(
                Message(
                    type=ECHO_REQUEST,
                    to=[alice_did],
                    body={
                        "message": "Hello from Bob!",
                        "timestamp": time.time_ns(),
                    },
                ),
                timeout=15.0,
            )
            body: dict[str, Any] = resp.unmarshal_body()
            echo_val = body.get("echo", "")
            if echo_val == "Hello from Bob!":
                pass_(f"echo response: {echo_val!r}")
            else:
                fail_("echo mismatch", f"got {echo_val!r}")
        except Exception as exc:
            fail_("echo request", str(exc))

        # ==================================================================
        section("W3C Credentials & Presentations")
        # ==================================================================

        # Test 5: Sign and verify a credential
        print("  [5] Sign and verify a credential")

        cred = Credential(
            context=["https://www.w3.org/ns/credentials/v2"],
            type=["VerifiableCredential"],
            credential_subject={"id": bob_did, "name": "Bob Test User"},
        )

        try:
            signed_cred = await alice.sign_credential(cred)
            if not signed_cred:
                fail_("sign credential", "sign_credential returned empty string")
            else:
                verified = await alice.verify_credential(signed_cred)
                if not verified.credential:
                    fail_("verify credential", "verified.credential is empty")
                elif "credentialSubject" not in verified.credential:
                    fail_("verify credential", "credentialSubject missing from verified credential")
                else:
                    pass_(
                        f"signed ({len(signed_cred)} chars) and verified credential "
                        f"with credentialSubject"
                    )
        except RESTError as exc:
            skip_(
                "sign/verify credential",
                f"{exc} (feature may not be deployed)",
            )
            signed_cred = ""
        except Exception as exc:
            fail_("sign/verify credential", str(exc))
            signed_cred = ""

        # Test 6: Store, list, and get credential
        print("  [6] Store, list, and get credential")

        if not signed_cred:
            skip_(
                "store/list/get credential",
                "skipped because sign_credential did not succeed",
            )
        else:
            try:
                stored = await alice.store_credential(signed_cred)
                if not stored.id:
                    fail_("store credential", "stored.id is empty")
                else:
                    creds = await alice.list_credentials()
                    if not creds:
                        fail_("list credentials", "list_credentials returned 0 credentials")
                    else:
                        found = any(c.id == stored.id for c in creds)
                        if not found:
                            fail_(
                                "list credentials",
                                f"stored credential {stored.id} not found in list of {len(creds)}",
                            )
                        else:
                            fetched = await alice.get_credential(stored.id)
                            if fetched.credential_jwt != signed_cred:
                                fail_(
                                    "get credential",
                                    "fetched credential JWT does not match stored value",
                                )
                            else:
                                pass_(
                                    f"stored (id={stored.id}), listed ({len(creds)} creds), "
                                    f"and fetched credential"
                                )
            except RESTError as exc:
                skip_(
                    "store/list/get credential",
                    f"{exc} (feature may not be deployed)",
                )
            except Exception as exc:
                fail_("store/list/get credential", str(exc))

        # Test 7: Sign and verify a presentation
        print("  [7] Sign and verify a presentation")

        if not signed_cred:
            skip_(
                "sign/verify presentation",
                "skipped because sign_credential did not succeed",
            )
        else:
            try:
                signed_pres = await alice.sign_presentation([signed_cred])
                if not signed_pres:
                    fail_("sign presentation", "sign_presentation returned empty string")
                else:
                    verified_pres = await alice.verify_presentation(signed_pres)
                    if not verified_pres.presentation:
                        fail_(
                            "verify presentation",
                            "verified_pres.presentation is empty",
                        )
                    else:
                        pass_(
                            f"signed ({len(signed_pres)} chars) and verified presentation"
                        )
            except RESTError as exc:
                skip_(
                    "sign/verify presentation",
                    f"{exc} (feature may not be deployed)",
                )
            except Exception as exc:
                fail_("sign/verify presentation", str(exc))

    finally:
        # ==================================================================
        # Cleanup
        # ==================================================================
        if bob is not None:
            try:
                await bob.close()
            except Exception:
                pass
        if alice is not None:
            try:
                await alice.close()
            except Exception:
                pass

    # ==================================================================
    # Summary
    # ==================================================================
    print()
    print(f"{COLOR_BOLD}{COLOR_CYAN}{'=' * 38}{COLOR_RESET}")
    print(f"  {COLOR_BOLD}{COLOR_GREEN}Passed:  {passed}{COLOR_RESET}")
    if failed > 0:
        print(f"  {COLOR_BOLD}{COLOR_RED}Failed:  {failed}{COLOR_RESET}")
    else:
        print(f"  Failed:  {failed}")
    if skipped > 0:
        print(f"  {COLOR_BOLD}{COLOR_YELLOW}Skipped: {skipped}{COLOR_RESET}")
    print(f"{COLOR_BOLD}{COLOR_CYAN}{'=' * 38}{COLOR_RESET}\n")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
