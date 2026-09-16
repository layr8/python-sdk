"""The node pushes a replacement delegated set to a live borrowed child.

These are the consumer-side boundary tests for that push. The frames are the
wire shape the node's own channel tests assert: a join reply whose
``delegated_credentials`` carries ``revision: 0``, the capability
``ephemeral_delegation_refresh/1``, and a push on the child's own topic with
event ``delegated_credentials`` and payload ``{revision, status, credentials}``.

Assertions read the attachments off the WIRE: the wallet is what reaches the
node, and a push that updated the reading but not the wallet would be two
answers to one question. As in ``test_grants.py``, the wallet reads through a
stub reader, because the REST base is derived from the socket URL.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from layr8 import Client, Config, Message, SDKError
from layr8.delegated import parse_delegation_push
from layr8.errors import ErrorKind
from layr8.wallet import Wallet

from .test_client import MockPhoenixServer, ws_url
from .test_wallet import jwt

PARENT = "did:web:acme.example:users:alice"
CHILD = f"{PARENT}:k7m2q9x4h3bd"
REFRESH = "ephemeral_delegation_refresh/1"
COVERING = [{"protocol": "*", "messageTypes": ["*"]}]


def child(tag: str) -> dict[str, Any]:
    """The child the node would mint for parent grant *tag*; its JWS ends in *tag*."""
    return {
        "id": f"child-of-{tag}",
        "parent_capability": f"urn:uuid:{tag}",
        "credential_jwt": jwt(
            {"id": f"child-of-{tag}", "credentialSubject": {"scope": COVERING}}, sig=f"sig{tag}"
        ),
    }


def reading(revision: Any, status: str, *tags: str) -> dict[str, Any]:
    return {"revision": revision, "status": status, "credentials": [child(t) for t in tags]}


def ids(r: Any) -> list[str] | None:
    return None if r is None else [c.id for c in r.credentials]


class RefreshNode:
    def __init__(self) -> None:
        self.server = MockPhoenixServer()
        self.capabilities = ["ephemeral_delegation/1", REFRESH]
        self.join_reading: dict[str, Any] | None = reading(0, "complete", "p1")
        self.topic = ""

    async def start(self) -> None:
        await self.server.start()

        def handler(msg: dict[str, Any]) -> None:
            if msg["event"] == "phx_join":
                self.topic = msg["topic"]
                response: dict[str, Any] = {"did": CHILD, "capabilities": self.capabilities}
                if self.join_reading is not None:
                    response["delegated_credentials"] = self.join_reading
                asyncio.ensure_future(self.server.send_to_client(
                    msg["ref"], msg["ref"], msg["topic"], "phx_reply",
                    {"status": "ok", "response": response},
                ))
            elif msg["ref"]:
                asyncio.ensure_future(self.server.send_to_client(
                    None, msg["ref"], msg["topic"], "phx_reply",
                    {"status": "ok", "response": {}},
                ))

        self.server.on_msg = handler

    async def push(self, payload: Any) -> None:
        await self.server.send_to_client(None, None, self.topic, "delegated_credentials", payload)
        await asyncio.sleep(0.05)

    def join_payloads(self) -> list[dict[str, Any]]:
        return [r["payload"] for r in self.server.get_received() if r["event"] == "phx_join"]

    def last_message_tags(self) -> list[str]:
        msgs = [r["payload"] for r in self.server.get_received() if r["event"] == "message"]
        atts = msgs[-1].get("attachments") or []
        return sorted(a["data"]["jws"].rsplit(".", 1)[1].removeprefix("sig") for a in atts)


@pytest.fixture
async def node():
    n = RefreshNode()
    await n.start()
    yield n
    await n.server.close()


def make_client(
    node: RefreshNode,
    *,
    parent: bool = True,
    reader: Any = None,
    on_error: Any = None,
) -> Client:
    cfg = Config(node_url=ws_url(node.server), api_key="test-api-key", agent_did=CHILD)
    if parent:
        cfg.parent_did = PARENT
    client = Client(cfg, on_error or (lambda _e: None))

    async def empty(_did: str) -> list[dict[str, Any]]:
        return []

    client._wallet = Wallet(reader or empty)
    return client


async def wire_tags(client: Client, node: RefreshNode) -> list[str]:
    await client.send(Message(type="https://layr8.io/protocols/echo/1.0/ping", to=["did:web:peer"]))
    return node.last_message_tags()


async def test_the_opt_in_is_sent_only_on_a_join_that_names_a_parent(node: RefreshNode) -> None:
    client = make_client(node)
    await client.connect()
    await client.close()
    assert node.join_payloads()[0]["delegation_refresh"] is True

    plain = make_client(node, parent=False)
    await plain.connect()
    await plain.close()
    assert "delegation_refresh" not in node.join_payloads()[1]


async def test_the_capability_and_its_absence_are_reported(node: RefreshNode) -> None:
    client = make_client(node)
    await client.connect()
    try:
        assert client.supports_ephemeral_delegation_refresh() is True
    finally:
        await client.close()

    node.capabilities = ["ephemeral_delegation/1"]
    old = make_client(node)
    await old.connect()
    try:
        assert old.supports_ephemeral_delegation_refresh() is False
        assert old.supports_ephemeral_delegation() is True
    finally:
        await old.close()


async def test_a_push_replaces_the_reading_and_the_wire_and_calls_on_delegation(
    node: RefreshNode,
) -> None:
    client = make_client(node)
    seen: list[tuple[str, list[str] | None]] = []
    client.on_delegation(lambda did, r: seen.append((did, ids(r))))
    await client.connect()
    try:
        assert await wire_tags(client, node) == ["p1"]

        await node.push(reading(1, "complete", "p2"))

        assert ids(client.delegated_credentials()) == ["child-of-p2"]
        assert seen == [(CHILD, ["child-of-p2"])]
        # Replaced, never appended.
        assert await wire_tags(client, node) == ["p2"]
    finally:
        await client.close()


async def test_complete_empty_is_applied_and_an_unread_join_is_corrected(node: RefreshNode) -> None:
    node.join_reading = reading(0, "unread")
    client = make_client(node)
    await client.connect()
    try:
        await node.push(reading(1, "partial", "p3"))
        r = client.delegated_credentials()
        assert r is not None and r.status == "partial"
        assert await wire_tags(client, node) == ["p3"]

        await node.push(reading(2, "complete"))
        r = client.delegated_credentials()
        assert r is not None and r.status == "complete" and r.credentials == []
        assert await wire_tags(client, node) == []
    finally:
        await client.close()


async def test_a_revision_that_is_not_newer_is_ignored(node: RefreshNode) -> None:
    client = make_client(node)
    seen: list[Any] = []
    await client.connect()
    try:
        await node.push(reading(2, "complete", "p2"))
        client.on_delegation(lambda did, r: seen.append(ids(r)))

        await node.push(reading(1, "complete", "stale"))
        await node.push(reading(2, "complete", "dup"))

        assert ids(client.delegated_credentials()) == ["child-of-p2"]
        assert seen == []
        assert await wire_tags(client, node) == ["p2"]
    finally:
        await client.close()


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [child("x")],
        {"revision": 1, "credentials": []},
        {"revision": 1, "status": "bogus", "credentials": []},
        {"revision": 1, "status": "complete"},
        {"status": "complete", "credentials": []},
        {"revision": "1", "status": "complete", "credentials": []},
        {"revision": -1, "status": "complete", "credentials": []},
        {"revision": 1.5, "status": "complete", "credentials": []},
        {"revision": True, "status": "complete", "credentials": []},
        # Never sent by the node: a failed read pushes nothing.
        {"revision": 1, "status": "unread", "credentials": []},
    ],
)
async def test_a_push_that_is_not_a_reading_is_ignored(node: RefreshNode, bad: Any) -> None:
    client = make_client(node)
    await client.connect()
    try:
        await node.push(bad)
        assert ids(client.delegated_credentials()) == ["child-of-p1"]
        assert await wire_tags(client, node) == ["p1"]
    finally:
        await client.close()


async def test_a_push_on_a_join_that_did_not_opt_in_is_ignored(node: RefreshNode) -> None:
    client = make_client(node, parent=False)
    await client.connect()
    try:
        assert ids(client.delegated_credentials()) == ["child-of-p1"]
        await node.push(reading(1, "complete"))
        assert ids(client.delegated_credentials()) == ["child-of-p1"]
    finally:
        await client.close()


async def test_a_raising_on_delegation_is_reported_and_reading_continues(node: RefreshNode) -> None:
    errors: list[SDKError] = []
    client = make_client(node, on_error=errors.append)

    def broken(_did: str, _r: Any) -> None:
        raise RuntimeError("listener broke")

    client.on_delegation(broken)
    await client.connect()
    try:
        await node.push(reading(1, "complete", "p2"))
        await node.push(reading(2, "complete", "p3"))
        assert [e.kind for e in errors] == [ErrorKind.HANDLER_EXCEPTION] * 2
        assert ids(client.delegated_credentials()) == ["child-of-p3"]
    finally:
        await client.close()


async def test_a_rejoin_starts_the_revision_again(node: RefreshNode) -> None:
    client = make_client(node)
    await client.connect()
    try:
        await node.push(reading(3, "complete", "p3"))
        assert ids(client.delegated_credentials()) == ["child-of-p3"]

        # What the reconnect loop calls, without timing a real socket drop.
        channel = client._channel
        assert channel is not None
        await channel._join(channel._protocols)
        assert ids(client.delegated_credentials()) == ["child-of-p1"]

        await node.push(reading(1, "complete", "p4"))
        assert ids(client.delegated_credentials()) == ["child-of-p4"]
    finally:
        await client.close()


async def test_a_send_racing_a_push_uses_one_whole_set(node: RefreshNode) -> None:
    node.join_reading = reading(0, "complete", "old-a", "old-b")
    release = asyncio.Event()
    parked = asyncio.Event()

    async def held(_did: str) -> list[dict[str, Any]]:
        if not release.is_set():
            parked.set()
            await release.wait()
        return []

    client = make_client(node, reader=held)
    await client.connect()
    try:
        sending = asyncio.ensure_future(wire_tags(client, node))
        await asyncio.wait_for(parked.wait(), 2)

        # The push lands while the send is waiting on its credential read.
        await node.push(reading(1, "complete", "new-a", "new-b"))
        release.set()

        assert await sending == ["old-a", "old-b"]
        assert await wire_tags(client, node) == ["new-a", "new-b"]
    finally:
        await client.close()


def test_pushed_readings_are_pairwise_distinct_and_unread_is_not_one() -> None:
    complete_empty = parse_delegation_push(reading(1, "complete"))
    complete_some = parse_delegation_push(reading(1, "complete", "a"))
    partial = parse_delegation_push(reading(1, "partial", "a"))

    assert complete_empty is not None and complete_empty[1] == 1
    assert len({repr(complete_empty), repr(complete_some), repr(partial)}) == 3
    assert parse_delegation_push(reading(1, "unread")) is None
