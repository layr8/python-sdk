"""Tests for layr8.mediation — a mock mediator on the Phoenix mock and a fake /didcomm ingress."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any

import pytest
from aiohttp import web

from layr8 import (
    Attachment,
    AttachmentData,
    Client,
    Config,
    ErrorKind,
    Message,
    RESTError,
    SDKError,
    mediation,
    post_didcomm,
)
from layr8.config import resolve_config
from layr8.mediation import (
    CM,
    DELIVERY_TYPE,
    PICKUP,
    ciphertext,
    mediator_path,
    own_registered,
)

from .test_client import MockPhoenixServer, mock_server, ws_url  # noqa: F401 — fixture

MEDIATOR = "did:web:node:mediator"
ALICE = "did:web:alice"
TOPIC = f"plugins:{ALICE}"
JWE = b'{"protected":"eyJ","ciphertext":"abc"}'
JWE_B64 = base64.urlsafe_b64encode(JWE).decode().rstrip("=")


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def test_own_registered() -> None:
    assert own_registered([{"recipient_did": ALICE, "result": "success"}], ALICE)
    assert own_registered([{"recipient_did": ALICE, "result": "no_change"}], ALICE)
    assert not own_registered([{"recipient_did": ALICE, "result": "client_error"}], ALICE)
    assert not own_registered([{"recipient_did": "did:web:bob", "result": "success"}], ALICE)
    assert not own_registered("nope", ALICE)


def test_ciphertext_decodes_base64url_without_padding() -> None:
    att = Attachment(id="m1", data=AttachmentData(base64=JWE_B64))
    assert ciphertext(att) == JWE
    assert ciphertext(Attachment(id="m2", data=AttachmentData(json={"n": 1}))) is None
    assert ciphertext(Attachment(id="m3", data=AttachmentData(base64="%%%"))) is None


def test_mediator_path() -> None:
    assert mediator_path(ALICE) == f"/api/v1/dids/{ALICE}/mediator"


def test_config_resolves_mediator_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAYR8_MEDIATOR_DID", MEDIATOR)
    monkeypatch.setenv("LAYR8_MEDIATOR_LIVE", "false")
    monkeypatch.setenv("LAYR8_DIDCOMM_URL", "https://edge.example/didcomm")
    cfg = resolve_config(Config(node_url="wss://n/plugin_socket/websocket", api_key="k"))
    assert cfg.mediator == MEDIATOR
    assert cfg.mediator_live is False
    assert cfg.didcomm_url == "https://edge.example/didcomm"

    for k in ("LAYR8_MEDIATOR_DID", "LAYR8_MEDIATOR_LIVE", "LAYR8_DIDCOMM_URL"):
        monkeypatch.delenv(k)
    cfg = resolve_config(Config(node_url="wss://n/plugin_socket/websocket", api_key="k", mediator=" "))
    assert cfg.mediator is None and cfg.mediator_live is True and cfg.didcomm_url is None


# --------------------------------------------------------------------------
# /didcomm ingress
# --------------------------------------------------------------------------


class FakeIngress:
    """A node's public /didcomm: records what was posted, answers 202 (or a set status)."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, bytes]] = []
        self.status = 202
        self.url = ""
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()

        async def didcomm(request: web.Request) -> web.Response:
            self.posts.append((request.headers.get("Content-Type", ""), await request.read()))
            if self.status >= 400:
                return web.json_response({"error": "refused"}, status=self.status)
            return web.Response(status=self.status)

        app.router.add_post("/didcomm", didcomm)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{port}/didcomm"

    async def close(self) -> None:
        if self._runner:
            await self._runner.cleanup()


@pytest.fixture
async def ingress():
    server = FakeIngress()
    await server.start()
    yield server
    await server.close()


async def test_post_didcomm_sends_ciphertext_without_api_key(ingress: FakeIngress) -> None:
    await post_didcomm(ingress.url, JWE)
    assert ingress.posts == [("application/didcomm-encrypted+json", JWE)]


async def test_post_didcomm_raises_rest_error_on_refusal(ingress: FakeIngress) -> None:
    ingress.status = 403
    with pytest.raises(RESTError) as exc:
        await post_didcomm(ingress.url, JWE)
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------
# A mock mediator behind the Phoenix mock
# --------------------------------------------------------------------------


class MockMediator:
    """Answers the coordinate-mediation/3.0 + messagepickup/3.0 requests the client makes."""

    def __init__(self, server: MockPhoenixServer, *, queued: list[dict[str, Any]] | None = None, deny: bool = False) -> None:
        self.server = server
        self.queued = list(queued or [])
        self.deny = deny
        self.requests: list[dict[str, Any]] = []
        self.joined_protocols: list[str] = []
        server.on_msg = self.on_msg

    def _reply(self, payload: dict[str, Any], msg_type: str, body: dict[str, Any], attachments: list[dict[str, Any]] | None = None) -> None:
        plaintext: dict[str, Any] = {
            "id": f"reply-{len(self.requests)}",
            "type": msg_type,
            "from": MEDIATOR,
            "to": [payload.get("from", "")],
            "thid": payload.get("thid", ""),
            "body": body,
        }
        if attachments:
            plaintext["attachments"] = attachments
        asyncio.ensure_future(
            self.server.send_to_client(None, None, TOPIC, "message", {"plaintext": plaintext})
        )

    def push_delivery(self, attachments: list[dict[str, Any]]) -> None:
        plaintext = {
            "id": "push-1",
            "type": DELIVERY_TYPE,
            "from": MEDIATOR,
            "to": [ALICE],
            "body": {"recipient_did": ALICE},
            "attachments": attachments,
        }
        asyncio.ensure_future(
            self.server.send_to_client(None, None, TOPIC, "message", {"plaintext": plaintext})
        )

    def on_msg(self, msg: dict[str, Any]) -> None:
        if msg["event"] == "phx_join":
            self.joined_protocols = list(msg["payload"].get("payload_types", []))
            asyncio.ensure_future(
                self.server.send_to_client(
                    msg["ref"], msg["ref"], msg["topic"], "phx_reply",
                    {"status": "ok", "response": {"did": ALICE}},
                )
            )
            return
        if msg.get("ref"):
            asyncio.ensure_future(
                self.server.send_to_client(None, msg["ref"], msg["topic"], "phx_reply", {"status": "ok", "response": {}})
            )
        if msg["event"] != "message":
            return
        payload = msg["payload"]
        self.requests.append(payload)
        t = payload.get("type", "")
        body = payload.get("body") or {}
        if t == f"{CM}mediate-request":
            if self.deny:
                self._reply(payload, f"{CM}mediate-deny", {})
            else:
                self._reply(payload, f"{CM}mediate-grant", {"routing_did": [MEDIATOR]})
        elif t == f"{CM}recipient-update":
            updated = [{"recipient_did": u["recipient_did"], "action": "add", "result": "success"} for u in body.get("updates", [])]
            self._reply(payload, f"{CM}recipient-update-response", {"updated": updated})
        elif t == f"{PICKUP}delivery-request":
            batch, self.queued = self.queued[: body.get("limit", 10)], self.queued[body.get("limit", 10):]
            if batch:
                self._reply(payload, DELIVERY_TYPE, {"recipient_did": ALICE}, batch)
            else:
                self._reply(payload, f"{PICKUP}status", {"message_count": 0})
        elif t == f"{PICKUP}messages-received":
            self._reply(payload, f"{PICKUP}status", {"message_count": len(self.queued)})
        elif t == f"{PICKUP}live-delivery-change":
            self._reply(payload, f"{PICKUP}status", {"message_count": 0, "live_delivery": body.get("live_delivery")})
        elif t == f"{PICKUP}status-request":
            self._reply(payload, f"{PICKUP}status", {"message_count": len(self.queued)})

    def types(self) -> list[str]:
        return [r.get("type", "") for r in self.requests]


class FakeRest:
    """Stands in for the node's REST API: records PUT/DELETE on the mediator path."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    async def put(self, path: str, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.calls.append(("PUT", path, body))
        return {"did": ALICE, "mediator": body.get("routing_did")}

    async def delete(self, path: str, **_: Any) -> dict[str, Any]:
        self.calls.append(("DELETE", path, None))
        return {}

    async def close(self) -> None:
        pass


def attachment(id: str, b64: str = JWE_B64) -> dict[str, Any]:
    return {"id": id, "media_type": "application/didcomm-encrypted+json", "data": {"base64": b64}}


async def _wait_for(pred: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def _client(mock_server: MockPhoenixServer, ingress: FakeIngress, errors: list[SDKError], **cfg: Any) -> Client:
    client = Client(
        Config(node_url=ws_url(mock_server), api_key="k", agent_did=ALICE, mediator=MEDIATOR, didcomm_url=ingress.url, **cfg),
        errors.append,
    )
    client._rest = FakeRest()  # type: ignore[assignment]
    return client


async def test_bootstrap_on_connect_enrols_declares_collects_and_goes_live(mock_server: MockPhoenixServer, ingress: FakeIngress) -> None:
    med = MockMediator(mock_server, queued=[attachment("m1"), attachment("m2")])
    errors: list[SDKError] = []
    client = _client(mock_server, ingress, errors)

    await client.connect()
    await _wait_for(lambda: f"{PICKUP}live-delivery-change" in med.types())

    # The delivery handler's protocol went out with the join.
    assert "https://didcomm.org/messagepickup/3.0" in med.joined_protocols
    # enrol → declare → pickup (one delivery round, ack) → drained → live on
    assert med.types() == [
        f"{CM}mediate-request",
        f"{CM}recipient-update",
        f"{PICKUP}delivery-request",
        f"{PICKUP}messages-received",
        f"{PICKUP}delivery-request",
        f"{PICKUP}live-delivery-change",
    ]
    assert med.requests[1]["body"] == {"updates": [{"recipient_did": ALICE, "action": "add"}]}
    assert med.requests[3]["body"] == {"message_id_list": ["m1", "m2"]}
    assert med.requests[5]["body"] == {"live_delivery": True}
    # Both ciphertexts were posted to the node's ingress, verbatim, no api key.
    assert ingress.posts == [("application/didcomm-encrypted+json", JWE)] * 2
    # Declared on the node.
    assert client._rest.calls == [("PUT", mediator_path(ALICE), {"routing_did": MEDIATOR})]  # type: ignore[attr-defined]
    assert errors == []
    await client.close()


async def test_live_delivery_push_is_reinjected_and_acknowledged(mock_server: MockPhoenixServer, ingress: FakeIngress) -> None:
    med = MockMediator(mock_server)
    errors: list[SDKError] = []
    client = _client(mock_server, ingress, errors, mediator_live=False)
    await client.connect()
    await _wait_for(lambda: f"{PICKUP}delivery-request" in med.types())
    assert f"{PICKUP}live-delivery-change" not in med.types()

    med.push_delivery([attachment("live-1")])
    await _wait_for(lambda: f"{PICKUP}messages-received" in med.types())
    ack = next(r for r in med.requests if r["type"] == f"{PICKUP}messages-received")
    assert ack["body"] == {"message_id_list": ["live-1"]}
    assert ingress.posts == [("application/didcomm-encrypted+json", JWE)]
    assert errors == []
    await client.close()


async def test_failed_reinjection_is_not_acknowledged(mock_server: MockPhoenixServer, ingress: FakeIngress) -> None:
    ingress.status = 500
    med = MockMediator(mock_server, queued=[attachment("m1")])
    errors: list[SDKError] = []
    client = _client(mock_server, ingress, errors, mediator_live=False)
    await client.connect()
    await _wait_for(lambda: f"{PICKUP}delivery-request" in med.types())
    await asyncio.sleep(0.2)
    # One delivery round, nothing went in, nothing acknowledged, loop stopped.
    assert med.types().count(f"{PICKUP}delivery-request") == 1
    assert f"{PICKUP}messages-received" not in med.types()
    assert errors == []
    await client.close()


async def test_mediate_deny_is_reported_as_a_mediation_error(mock_server: MockPhoenixServer, ingress: FakeIngress) -> None:
    med = MockMediator(mock_server, deny=True)
    errors: list[SDKError] = []
    client = _client(mock_server, ingress, errors)
    await client.connect()
    await _wait_for(lambda: len(errors) == 1)
    assert errors[0].kind is ErrorKind.MEDIATION
    assert errors[0].type == "enroll"
    assert errors[0].from_did == MEDIATOR
    assert "mediate_denied" in str(errors[0].cause)
    assert med.types() == [f"{CM}mediate-request"]
    await client.close()


async def test_steps_by_hand_return_results_and_never_raise(mock_server: MockPhoenixServer, ingress: FakeIngress) -> None:
    med = MockMediator(mock_server, queued=[attachment("m1")])
    errors: list[SDKError] = []
    client = Client(Config(node_url=ws_url(mock_server), api_key="k", agent_did=ALICE), errors.append)
    client._rest = FakeRest()  # type: ignore[assignment]
    await client.connect()
    assert client.mediator is None

    e = await mediation.enroll(client, MEDIATOR, recipients=["did:web:alice:worker"])
    assert e.ok and e.routing_did == [MEDIATOR]
    assert med.requests[-1]["body"]["updates"] == [
        {"recipient_did": ALICE, "action": "add"},
        {"recipient_did": "did:web:alice:worker", "action": "add"},
    ]
    assert (await mediation.declare(client, MEDIATOR)).ok
    s = await mediation.status(client, MEDIATOR)
    assert s.ok and s.status == {"message_count": 1}
    p = await mediation.pickup(client, MEDIATOR, didcomm_url=ingress.url)
    assert p.ok and p.collected == 1
    lv = await mediation.live(client, MEDIATOR, True)
    assert lv.ok and lv.status["live_delivery"] is True
    assert (await mediation.undeclare(client)).ok
    assert client._rest.calls[-1] == ("DELETE", mediator_path(ALICE), None)  # type: ignore[attr-defined]

    # A mediator that never answers: a result, not an exception.
    med.server.on_msg = lambda m: None
    r = await mediation.status(client, MEDIATOR, timeout=0.2)
    assert not r.ok and r.error
    await client.close()


async def test_no_mediator_registers_no_delivery_handler(mock_server: MockPhoenixServer) -> None:
    client = Client(Config(node_url=ws_url(mock_server), api_key="k", agent_did=ALICE), lambda e: None)
    assert client._registry.lookup(DELIVERY_TYPE) is None
    assert client.didcomm_url == f"http://127.0.0.1:{mock_server.port}/didcomm"
