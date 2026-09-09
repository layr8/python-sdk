"""A DID that borrows a parent's authority: its name, and what the join learns.

The mock server here keeps the RAW frame, not a parsed one, because the promise
this feature makes to every caller that will never use it is about bytes: a join
that names no parent must put on the socket exactly the payload it put there
before the field existed.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
import websockets
import websockets.asyncio.server

from layr8 import (
    CHILD_SEGMENT_LENGTH,
    Client,
    Config,
    DelegatedCredential,
    DelegatedCredentialsReading,
    Layr8Error,
    SDKError,
    Wallet,
    did_namespace_of,
    is_beneath_parent,
    random_child_segment,
    resolve_borrower_did,
)
from layr8.child_did import CHILD_SEGMENT_ALPHABET
from layr8.config import resolve_config
from layr8.delegated import parse_delegated_credentials

PARENT = "did:web:acme.example:users:alice"


def _discard_errors(err: SDKError) -> None:
    pass


# --- the name, and who chose it ---------------------------------------------


def test_no_parent_leaves_the_did_alone_and_names_nobody() -> None:
    got = resolve_borrower_did("did:web:acme.example:agents:bot", "")
    assert got.did == "did:web:acme.example:agents:bot"
    # This rule is about a relationship between two names, and there is only
    # one name here. Reporting "client" would claim a caller chose a BORROWER's
    # name when there is no borrower.
    assert got.child_name_source == ""


def test_a_parent_with_no_did_derives_one_exactly_one_segment_beneath() -> None:
    got = resolve_borrower_did("", PARENT)
    assert is_beneath_parent(got.did, PARENT)
    segment = got.did[len(PARENT) + 1:]
    assert ":" not in segment
    assert len(segment) == CHILD_SEGMENT_LENGTH
    assert got.child_name_source == "sdk"


def test_a_caller_supplied_name_beneath_the_parent_is_reported_as_the_callers() -> None:
    child = f"{PARENT}:handbuilt"
    got = resolve_borrower_did(child, PARENT)
    assert got.did == child
    assert got.child_name_source == "client"


def test_a_did_not_beneath_its_parent_fails_locally() -> None:
    with pytest.raises(Layr8Error) as excinfo:
        resolve_borrower_did("did:web:acme.example:agents:bot", PARENT)
    # The message has to carry the way out, or the reader is left with a rule
    # and no repair.
    assert "leave agent_did empty" in str(excinfo.value)


@pytest.mark.parametrize(
    "child,expected",
    [
        (f"{PARENT}:k7m2q9x4h3bd", True),
        (PARENT, False),
        ("did:web:acme.example:users:alicent", False),
        (f"{PARENT}:k7m2:q9x4", False),
        (f"{PARENT}:", False),
        ("did:web:other.example:bot", False),
        ("", False),
    ],
)
def test_is_beneath_parent(child: str, expected: bool) -> None:
    assert is_beneath_parent(child, PARENT) is expected


def test_an_empty_parent_is_beneath_nothing() -> None:
    assert is_beneath_parent(f"{PARENT}:x", "") is False


def test_did_namespace_of_is_the_key_entry_covering_every_borrower() -> None:
    assert did_namespace_of(PARENT) == f"{PARENT}:*"


def test_random_child_segment_uses_the_crockford_alphabet_and_varies() -> None:
    seen = set()
    for _ in range(64):
        segment = random_child_segment()
        assert len(segment) == CHILD_SEGMENT_LENGTH
        assert set(segment) <= set(CHILD_SEGMENT_ALPHABET)
        # i, l, o and u are omitted so the value survives being read off a
        # screen and typed back.
        assert not set(segment) & set("ilou")
        seen.add(segment)
    # A counter or a constant would collide here, and a collision is one
    # connection joining onto another's identity.
    assert len(seen) == 64


def test_the_three_child_name_sources_are_pairwise_distinct() -> None:
    """The THREE values, not "the field was set".

    A generated name and a hand-built conforming one are identical bytes on the
    socket; folding "not stated" into "client" would claim a caller chose a name
    when nothing measured that.
    """
    derived = resolve_borrower_did("", PARENT).child_name_source
    supplied = resolve_borrower_did(f"{PARENT}:x", PARENT).child_name_source
    not_stated = resolve_borrower_did("did:web:acme.example:agents:bot", "").child_name_source

    assert derived != supplied
    assert derived != not_stated
    assert supplied != not_stated


# --- the configuration ------------------------------------------------------


def test_resolve_config_settles_the_borrower_did_so_agent_did_is_the_one_that_joins() -> None:
    cfg = resolve_config(
        Config(node_url="ws://node.example/plugin_socket/websocket", api_key="k", parent_did=PARENT)
    )
    assert is_beneath_parent(cfg.agent_did, PARENT)
    assert cfg.child_name_source == "sdk"
    assert cfg.parent_did == PARENT


def test_the_client_refuses_a_did_not_beneath_its_parent_before_anything_is_written() -> None:
    with pytest.raises(Layr8Error):
        Client(
            Config(
                node_url="ws://127.0.0.1:1/plugin_socket/websocket",
                api_key="k",
                agent_did="did:web:acme.example:agents:bot",
                parent_did=PARENT,
            ),
            _discard_errors,
        )


# --- the wire ---------------------------------------------------------------


class RawFrameServer:
    """A Phoenix mock that keeps the frames exactly as they arrived."""

    def __init__(self, join_response: dict[str, Any] | None = None) -> None:
        self._server: websockets.asyncio.server.Server | None = None
        self._ws: websockets.asyncio.server.ServerConnection | None = None
        self.raw_frames: list[str] = []
        self.port = 0
        self._join_response = join_response if join_response is not None else {}

    async def start(self) -> None:
        self._server = await websockets.asyncio.server.serve(self._handler, "127.0.0.1", 0)
        self.port = list(self._server.sockets)[0].getsockname()[1]

    async def _handler(self, ws: websockets.asyncio.server.ServerConnection) -> None:
        self._ws = ws
        try:
            async for raw in ws:
                self.raw_frames.append(raw)
                arr = json.loads(raw)
                if arr[3] == "phx_join":
                    await ws.send(
                        json.dumps(
                            [arr[1], arr[1], arr[2], "phx_reply",
                             {"status": "ok", "response": self._join_response}]
                        )
                    )
        except websockets.exceptions.ConnectionClosed:
            pass

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/plugin_socket/websocket"

    def join_payload_raw(self) -> str:
        for raw in self.raw_frames:
            arr = json.loads(raw)
            if arr[3] == "phx_join":
                # The payload, re-serialised from the frame the client actually
                # wrote — key order and all, because json.loads preserves it.
                return json.dumps(arr[4])
        raise AssertionError("no phx_join reached the server")

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()


async def _join_payload(config: Config, join_response: dict[str, Any] | None = None) -> str:
    server = RawFrameServer(join_response)
    await server.start()
    config.node_url = server.url
    client = Client(config, _discard_errors)
    await client.connect()
    try:
        return server.join_payload_raw()
    finally:
        await client.close()
        await server.close()


async def test_a_join_that_names_no_parent_is_unchanged_on_the_wire() -> None:
    """Pins the EXACT payload, not "parentDid is absent".

    The promise this feature makes to every existing caller is that a join
    naming no parent puts the payload on the socket it put there before the
    field existed, so the literal is the assertion.
    """
    want = json.dumps(
        {
            "payload_types": [],
            "reply_protocol": True,
            "did_spec": {
                "mode": "Create",
                "storage": "persistent",
                "type": "plugin",
                "verificationMethods": [
                    {"purpose": "authentication"},
                    {"purpose": "assertionMethod"},
                    {"purpose": "keyAgreement"},
                ],
            },
        }
    )
    got = await _join_payload(
        Config(node_url="", api_key="k", agent_did="did:web:acme.example:agents:bot")
    )
    assert got == want


async def test_naming_a_parent_adds_exactly_two_keys_and_forces_ephemeral_storage() -> None:
    got = json.loads(await _join_payload(Config(node_url="", api_key="k", parent_did=PARENT)))
    spec = got["did_spec"]

    assert spec["parentDid"] == PARENT
    assert spec["childNameSource"] == "sdk"
    # Only a temporary identity may borrow: the node refuses a join naming a
    # parent that declares "persistent" with
    # e.join.plugin.child.storage-not-ephemeral. A borrowed DID is a fixed
    # identity by construction, so the fixed-identity rule would make every
    # borrowed join unreachable.
    assert spec["storage"] == "ephemeral"
    assert set(spec) - {"parentDid", "childNameSource"} == {
        "mode", "storage", "type", "verificationMethods"
    }


async def test_child_name_source_is_absent_rather_than_empty_when_nobody_chose_a_name() -> None:
    got = json.loads(
        await _join_payload(Config(node_url="", api_key="k", agent_did="did:web:acme.example:agents:bot"))
    )
    spec = got["did_spec"]
    assert "childNameSource" not in spec
    assert "parentDid" not in spec


async def test_a_caller_built_name_is_reported_as_the_callers_on_the_wire() -> None:
    got = json.loads(
        await _join_payload(
            Config(node_url="", api_key="k", agent_did=f"{PARENT}:handbuilt", parent_did=PARENT)
        )
    )
    assert got["did_spec"]["childNameSource"] == "client"
    assert got["did_spec"]["storage"] == "ephemeral"


async def test_the_client_speaks_as_the_derived_did() -> None:
    server = RawFrameServer()
    await server.start()
    client = Client(Config(node_url=server.url, api_key="k", parent_did=PARENT), _discard_errors)
    await client.connect()
    try:
        assert is_beneath_parent(client.did, PARENT)
    finally:
        await client.close()
        await server.close()


# --- the four readings ------------------------------------------------------


def test_the_four_delegation_readings_are_pairwise_distinct() -> None:
    """The assertion this whole object exists for.

    The fourth reading is the ABSENT key — a node that never looked. It is not a
    fourth flavour of "read and found nothing", and an unreadable wallet is not
    an empty one: both of those collapses land on the reassuring answer, and a
    client acting on it sends its messages bare.

    Asserting that any three of them differ passes while the defect is present,
    so every pair is compared.
    """
    readings = {
        "absent — the node never looked": parse_delegated_credentials(None),
        "read, and the parent grants nothing": parse_delegated_credentials(
            {"status": "complete", "credentials": []}
        ),
        "read, and some could not be delegated": parse_delegated_credentials(
            {
                "status": "partial",
                "credentials": [
                    {"id": "urn:uuid:1", "parent_capability": "urn:uuid:p", "credential_jwt": "a.b.c"}
                ],
            }
        ),
        "could not be read at all": parse_delegated_credentials(
            {"status": "unread", "credentials": []}
        ),
    }

    names = list(readings)
    for i, left in enumerate(names):
        for right in names[i + 1:]:
            assert readings[left] != readings[right], (
                f"{left!r} and {right!r} are the same value; "
                "one of them states something nobody measured"
            )

    # And each one is the value it claims to be, so "distinct" is not being
    # satisfied by four different wrong answers.
    assert readings["absent — the node never looked"] is None
    assert readings["read, and the parent grants nothing"] == DelegatedCredentialsReading(
        status="complete", credentials=[]
    )
    assert readings["could not be read at all"].status == "unread"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [{"id": "urn:uuid:1"}],
        {"status": "partially", "credentials": []},
        {"status": "complete"},
        {"status": "complete", "credentials": {}},
        "complete",
    ],
    ids=[
        "absent",
        "a bare list — an older node",
        "a status this build does not know",
        "credentials missing",
        "credentials not a list",
        "a string",
    ],
)
def test_nothing_but_a_well_formed_reading_is_a_reading(raw: Any) -> None:
    assert parse_delegated_credentials(raw) is None


def test_a_reading_carries_the_credentials_verbatim() -> None:
    got = parse_delegated_credentials(
        {
            "status": "complete",
            "credentials": [
                {"id": "urn:uuid:1", "parent_capability": "urn:uuid:p", "credential_jwt": "a.b.c"}
            ],
        }
    )
    assert got is not None
    assert got.credentials == [
        DelegatedCredential(id="urn:uuid:1", parent_capability="urn:uuid:p", credential_jwt="a.b.c")
    ]


# --- the wallet -------------------------------------------------------------


def _b64(value: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _delivered_grant(sig: str) -> DelegatedCredential:
    """A delegated credential whose JWT is a real grant.

    Built as a grant so the wallet's own parser accepts it exactly as it accepts
    one read over REST — no special case for a delivered credential.
    """
    claims = {
        "id": f"urn:uuid:delegated-{sig}",
        "credentialSubject": {"scope": [{"protocol": "*", "messageTypes": ["*"]}]},
    }
    jwt = f"{_b64({'alg': 'EdDSA'})}.{_b64(claims)}.{sig}"
    return DelegatedCredential(
        id=f"urn:uuid:delegated-{sig}", parent_capability="urn:uuid:parent", credential_jwt=jwt
    )


async def test_credentials_from_the_join_reply_are_attached_though_the_node_stores_none() -> None:
    # GET /api/v1/credentials returns nothing for a borrowed DID, forever: the
    # node stores nothing about a credential that belongs to a connection.
    async def read_nothing(did: str) -> list[dict[str, Any]]:
        return []

    wallet = Wallet(read_nothing)
    wallet.seed_delivered("did:web:child", [_delivered_grant("sigA")])

    attachments = await wallet.attachments_for(
        "did:web:child",
        recipients=["did:web:peer"],
        type_uri="https://layr8.io/protocols/echo/1.0/ping",
    )
    assert len(attachments) == 1


async def test_delivered_credentials_survive_a_failed_read_of_a_source_that_never_has_them() -> None:
    class ReadFailed(Exception):
        pass

    async def read_fails(did: str) -> list[dict[str, Any]]:
        raise ReadFailed("credentials endpoint unavailable")

    wallet = Wallet(read_fails)
    wallet.seed_delivered("did:web:child", [_delivered_grant("sigA")])

    attachments = await wallet.attachments_for(
        "did:web:child",
        recipients=["did:web:peer"],
        type_uri="https://layr8.io/protocols/echo/1.0/ping",
    )
    assert len(attachments) == 1

    # With nothing delivered, the same failure IS the only answer this DID has
    # and the caller is told.
    with pytest.raises(ReadFailed):
        await wallet.attachments_for(
            "did:web:other",
            recipients=["did:web:peer"],
            type_uri="https://layr8.io/protocols/echo/1.0/ping",
        )


async def test_a_rejoin_replaces_what_the_previous_join_delivered() -> None:
    async def read_nothing(did: str) -> list[dict[str, Any]]:
        return []

    wallet = Wallet(read_nothing)
    wallet.seed_delivered("did:web:child", [_delivered_grant("sigA")])
    wallet.seed_delivered("did:web:child", [_delivered_grant("sigB")])

    held = wallet.delivered_to("did:web:child")
    assert len(held) == 1, "a rejoin replaces, it does not merge"
    assert held[0].id == "urn:uuid:delegated-sigB"


async def test_a_join_with_no_reading_clears_what_the_previous_one_delivered() -> None:
    """The case the unconditional callback exists for.

    The node mints a fresh set per join, so a rejoin carrying no reading is a
    rejoin after which the previous set must go. Keeping it leaves the wallet
    attaching the last join's credentials while delegated_credentials() reports
    there are none.
    """
    async def read_nothing(did: str) -> list[dict[str, Any]]:
        return []

    wallet = Wallet(read_nothing)
    client = Client(
        Config(node_url="ws://127.0.0.1:1/plugin_socket/websocket", api_key="k", agent_did="did:web:child"),
        _discard_errors,
    )
    client._wallet = wallet

    client._apply_delegated(
        "did:web:child",
        DelegatedCredentialsReading(status="complete", credentials=[_delivered_grant("sigA")]),
    )
    assert len(wallet.delivered_to("did:web:child")) == 1

    client._apply_delegated("did:web:child", None)
    assert wallet.delivered_to("did:web:child") == []


async def test_an_unread_wallet_delivers_nothing_rather_than_an_empty_measurement() -> None:
    async def read_nothing(did: str) -> list[dict[str, Any]]:
        return []

    wallet = Wallet(read_nothing)
    wallet.seed_delivered("did:web:child", [])
    assert wallet.delivered_to("did:web:child") == []


async def test_an_entry_that_is_not_a_grant_is_dropped_at_seed_time_not_send_time() -> None:
    async def read_nothing(did: str) -> list[dict[str, Any]]:
        return []

    wallet = Wallet(read_nothing)
    wallet.seed_delivered(
        "did:web:child",
        [DelegatedCredential(id="urn:uuid:1", credential_jwt="not-a-compact-jws"), _delivered_grant("sigA")],
    )
    assert len(wallet.delivered_to("did:web:child")) == 1


# --- the join reply, end to end ---------------------------------------------


async def test_the_join_reply_reading_reaches_the_client() -> None:
    response = {
        "did": "did:web:node:test",
        "capabilities": ["ephemeral_delegation/1"],
        "delegated_credentials": {
            "status": "partial",
            "credentials": [
                {"id": "urn:uuid:1", "parent_capability": "urn:uuid:p", "credential_jwt": "a.b.c"}
            ],
        },
    }
    server = RawFrameServer(response)
    await server.start()
    client = Client(Config(node_url=server.url, api_key="k", parent_did=PARENT), _discard_errors)
    await client.connect()
    try:
        assert client.supports_ephemeral_delegation() is True
        reading = client.delegated_credentials()
        assert reading is not None
        assert reading.status == "partial"
        assert len(reading.credentials) == 1
    finally:
        await client.close()
        await server.close()


async def test_a_reply_carrying_no_reading_leaves_the_client_with_none() -> None:
    """No reading, and the node never advertised it either — two different
    things from the caller's side, and both must stay reachable."""
    server = RawFrameServer({"did": "did:web:node:test"})
    await server.start()
    client = Client(Config(node_url=server.url, api_key="k", parent_did=PARENT), _discard_errors)
    await client.connect()
    try:
        assert client.delegated_credentials() is None
        assert client.supports_ephemeral_delegation() is False
    finally:
        await client.close()
        await server.close()
