"""Tests for layr8.wallet — grant decoding, scope matching, the cap, the cache."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from aiohttp import web

from layr8.wallet import (
    MAX_ATTACHED,
    HeldCredential,
    Wallet,
    parse_credential,
    rest_credential_reader,
    select_for,
    split_type_uri,
    tool_name_of,
)
from layr8.rest import RestClient


def _b64(obj: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def jwt(claims: dict[str, Any], sig: str = "sig") -> str:
    """A compact JWS whose payload is *claims*.

    The signature segment is what the wallet falls back to for an attachment id,
    so it varies per fixture.
    """
    return f"{_b64({'alg': 'EdDSA'})}.{_b64(claims)}.{sig}"


def grant_record(
    *,
    scope: list[dict[str, Any]] | None = None,
    cred_id: str | None = "urn:uuid:grant-1",
    tools: list[str] | None = None,
    sig: str = "sig",
    valid_until: str | None = None,
) -> dict[str, Any]:
    subject: dict[str, Any] = {
        "scope": scope if scope is not None else [{"protocol": "*", "messageTypes": ["*"]}]
    }
    if tools is not None:
        subject["grant"] = {"tools": tools}

    claims: dict[str, Any] = {"credentialSubject": subject}
    if cred_id is not None:
        claims["id"] = cred_id

    rec: dict[str, Any] = {"credential_jwt": jwt(claims, sig)}
    if valid_until is not None:
        rec["valid_until"] = valid_until
    return rec


def held(**kwargs: Any) -> HeldCredential:
    return parse_credential(grant_record(**kwargs))  # type: ignore[return-value]


class TestSplitTypeUri:
    def test_splits_on_the_last_slash(self) -> None:
        """What the node's own parser does — protocol and message type match separately."""
        assert split_type_uri("https://layr8.io/protocols/mcp/1.0/tools-call") == (
            "https://layr8.io/protocols/mcp/1.0",
            "tools-call",
        )

    def test_a_type_with_no_slash_is_all_protocol(self) -> None:
        assert split_type_uri("bare") == ("bare", "")


class TestToolNameOf:
    def test_reads_params_name(self) -> None:
        assert tool_name_of({"params": {"name": "send_email"}}) == "send_email"

    def test_none_for_a_body_carrying_no_tool(self) -> None:
        assert tool_name_of({"text": "hi"}) is None
        assert tool_name_of(None) is None
        assert tool_name_of({"params": "not-a-dict"}) is None


class TestParseCredential:
    def test_decodes_a_grant_with_top_level_claims(self) -> None:
        cred = held(cred_id="urn:uuid:g1", tools=["a"])
        assert cred.id == "urn:uuid:g1"
        assert cred.tools == ["a"]
        assert cred.scope == [{"protocol": "*", "messageTypes": ["*"]}]

    def test_decodes_a_grant_wrapped_in_the_standard_vc_envelope(self) -> None:
        claims = {
            "vc": {
                "id": "urn:uuid:wrapped",
                "credentialSubject": {"scope": [{"protocol": "*", "messageTypes": ["*"]}]},
            }
        }
        cred = parse_credential({"credential_jwt": jwt(claims)})
        assert cred is not None and cred.id == "urn:uuid:wrapped"

    def test_a_vrtc_is_not_a_grant(self) -> None:
        """A VRTC has `grantable` instead of `scope` — the node's control chain, not this."""
        claims = {"credentialSubject": {"grantable": [{"protocol": "*"}]}}
        assert parse_credential({"credential_jwt": jwt(claims)}) is None

    @pytest.mark.parametrize(
        "raw",
        ["not.a.jwt.at.all", "two.parts", "head.payload.", ""],
    )
    def test_anything_but_a_three_segment_compact_jws_is_refused(self, raw: str) -> None:
        # Anything else cannot be verified by the node, so putting it on the
        # wire only costs a denial that names the wrong problem.
        assert parse_credential({"credential_jwt": raw}) is None

    def test_no_credential_jwt_at_all(self) -> None:
        assert parse_credential({}) is None

    def test_falls_back_to_the_signature_segment_so_two_grants_never_collide(self) -> None:
        # Every credential from one issuer shares a header, so falling back to
        # the head of the JWT gave them all the same attachment id — and a frame
        # carrying two attachments with one id is a frame whose second
        # attachment may not survive.
        a = held(cred_id=None, sig="signature-a")
        b = held(cred_id=None, sig="signature-b")
        assert a.id != b.id
        assert a.id.startswith("urn:jws:")


def select_one(scope: list[dict[str, Any]], recipient: str, type_uri: str) -> list[Any]:
    return select_for([held(scope=scope)], recipients=[recipient], type_uri=type_uri)


class TestScopeMatching:
    """select_for mirrors the node's authorization policy."""

    def test_wildcard_protocol_and_message_type_cover_anything(self) -> None:
        assert select_one(
            [{"protocol": "*", "messageTypes": ["*"]}],
            "did:web:bob",
            "https://layr8.io/protocols/mcp/1.0/tools-call",
        )

    def test_a_non_matching_protocol_covers_nothing(self) -> None:
        assert not select_one(
            [{"protocol": "https://other.example/proto/1.0", "messageTypes": ["*"]}],
            "did:web:bob",
            "https://layr8.io/protocols/mcp/1.0/tools-call",
        )

    def test_a_non_matching_message_type_covers_nothing(self) -> None:
        assert not select_one(
            [{"protocol": "https://layr8.io/protocols/mcp/1.0", "messageTypes": ["ping"]}],
            "did:web:bob",
            "https://layr8.io/protocols/mcp/1.0/tools-call",
        )

    def test_an_exact_resource_matches(self) -> None:
        assert select_one(
            [{"protocol": "*", "messageTypes": ["*"], "resource": "did:web:bob"}],
            "did:web:bob",
            "p/t",
        )

    def test_star_suffix_covers_under_the_slash_but_not_a_longer_word(self) -> None:
        """The rego strips only the `*`, so the trailing slash is part of the prefix."""
        star = [{"protocol": "*", "messageTypes": ["*"], "resource": "tables/*"}]
        assert select_one(star, "tables/customers", "p/t")
        assert not select_one(star, "tablesarchive", "p/t")

    def test_a_bare_resource_is_a_segment_prefix(self) -> None:
        # The clause whose absence points the wrong way: this side withholding a
        # grant the policy would have honoured, which costs a working call and
        # shows up as "no grant covers this call".
        bare = [{"protocol": "*", "messageTypes": ["*"], "resource": "tables"}]
        assert select_one(bare, "tables/customers", "p/t")
        assert select_one(bare, "tables", "p/t")
        assert not select_one(bare, "tables_archive", "p/t")

    def test_a_credential_covering_any_recipient_goes_on_the_wire(self) -> None:
        """The node evaluates one decision per recipient."""
        cred = held(scope=[{"protocol": "*", "messageTypes": ["*"], "resource": "did:web:bob"}])
        assert select_for(
            [cred], recipients=["did:web:alice", "did:web:bob"], type_uri="p/t"
        )


class TestAttachmentShape:
    def test_media_type_is_exactly_vc_jwt_and_the_jws_rides_in_data_jws(self) -> None:
        # `media_type` is the ONLY thing the node's credential extractor filters
        # on, by exact string equality; everything else is dropped silently and
        # the denial is byte-for-byte the one for attaching nothing.
        rec = grant_record()
        (att,) = select_for(
            [parse_credential(rec)],  # type: ignore[list-item]
            recipients=["x"],
            type_uri="p/t",
        )
        assert att.media_type == "application/vc+jwt"
        assert att.data.jws == rec["credential_jwt"]
        assert att.data.base64 == ""

    def test_it_survives_marshalling_onto_the_wire(self) -> None:
        from layr8.message import Message, marshal_didcomm

        rec = grant_record()
        atts = select_for(
            [parse_credential(rec)],  # type: ignore[list-item]
            recipients=["x"],
            type_uri="p/t",
        )
        env = marshal_didcomm(Message(id="m", type="p/t", attachments=atts))
        assert env["attachments"][0]["media_type"] == "application/vc+jwt"
        assert env["attachments"][0]["data"] == {"jws": rec["credential_jwt"]}


class TestTheCap:
    def test_attaches_at_most_max_attached_and_says_what_was_left_off(self) -> None:
        creds = [held(cred_id=f"urn:uuid:g{i}", sig=f"sig{i}") for i in range(20)]
        capped: list[dict[str, int]] = []

        atts = select_for(
            creds,
            recipients=["did:web:bob"],
            type_uri="p/t",
            on_capped=capped.append,
        )

        assert len(atts) == MAX_ATTACHED
        assert capped == [{"covering": 20, "attached": 16}]

    def test_no_callback_when_everything_fits(self) -> None:
        capped: list[dict[str, int]] = []
        select_for([held()], recipients=["x"], type_uri="p/t", on_capped=capped.append)
        assert capped == []

    def test_a_grant_naming_this_tool_outranks_one_naming_another(self) -> None:
        # `grant.tools` is not a policy input anywhere, so it never filters — it
        # only decides who keeps a slot when the cap bites.
        others = [
            held(cred_id=f"urn:uuid:other{i}", sig=f"s{i}", tools=["other"])
            for i in range(MAX_ATTACHED)
        ]
        wanted = held(cred_id="urn:uuid:wanted", sig="sw", tools=["send_email"])

        atts = select_for(
            [*others, wanted],
            recipients=["did:web:bob"],
            type_uri="p/t",
            tool="send_email",
        )

        assert atts[0].id == "urn:uuid:wanted"

    def test_a_certainly_lapsed_grant_loses_its_slot_to_a_live_one(self) -> None:
        lapsed = [
            held(cred_id=f"urn:uuid:old{i}", sig=f"o{i}", valid_until="2020-01-01T00:00:00Z")
            for i in range(MAX_ATTACHED)
        ]
        live = held(cred_id="urn:uuid:live", sig="lv")

        atts = select_for([*lapsed, live], recipients=["did:web:bob"], type_uri="p/t")
        assert atts[0].id == "urn:uuid:live"

    def test_an_expired_grant_is_still_attached_when_there_is_room(self) -> None:
        # Validity is the PDP's call, made against a clock this side cannot see.
        # Withholding because a local clock thought it was dead costs a working
        # call, and that failure is silent.
        expired = held(valid_until="2020-01-01T00:00:00Z")
        assert select_for([expired], recipients=["did:web:bob"], type_uri="p/t")


class TestTheCache:
    def _counting_reader(self, result: Any):
        calls = {"n": 0}

        async def reader(_did: str) -> Any:
            calls["n"] += 1
            if isinstance(result, Exception):
                raise result
            return result

        return reader, calls

    async def test_a_successful_read_is_cached_then_re_read(self) -> None:
        reader, calls = self._counting_reader([grant_record()])
        wallet = Wallet(reader, ttl_ms=1_000)

        assert len(await wallet.held_by("did:web:alice", now_ms=0)) == 1
        await wallet.held_by("did:web:alice", now_ms=999)
        assert calls["n"] == 1

        await wallet.held_by("did:web:alice", now_ms=1_000)
        assert calls["n"] == 2

    async def test_refresh_drops_the_entry(self) -> None:
        reader, calls = self._counting_reader([grant_record()])
        wallet = Wallet(reader, ttl_ms=60_000)

        await wallet.held_by("did:web:alice", now_ms=0)
        wallet.refresh("did:web:alice")
        await wallet.held_by("did:web:alice", now_ms=1)
        assert calls["n"] == 2

    async def test_refresh_with_no_did_drops_everything(self) -> None:
        reader, calls = self._counting_reader([grant_record()])
        wallet = Wallet(reader, ttl_ms=60_000)

        await wallet.held_by("did:web:alice", now_ms=0)
        await wallet.held_by("did:web:bob", now_ms=0)
        wallet.refresh()
        await wallet.held_by("did:web:alice", now_ms=1)
        await wallet.held_by("did:web:bob", now_ms=1)
        assert calls["n"] == 4

    async def test_a_failure_is_cached_so_a_bad_api_key_is_not_a_per_message_round_trip(
        self,
    ) -> None:
        reader, calls = self._counting_reader(RuntimeError("unauthorized"))
        wallet = Wallet(reader, ttl_ms=60_000, read_timeout_ms=2_000)

        for now in (0, 1_000):
            with pytest.raises(RuntimeError):
                await wallet.held_by("did:web:alice", now_ms=now)
        assert calls["n"] == 1

        # ...and it lapses, because the fix for whatever broke it should take
        # effect without a restart. Default failure TTL here is 5s.
        with pytest.raises(RuntimeError):
            await wallet.held_by("did:web:alice", now_ms=5_000)
        assert calls["n"] == 2

    async def test_the_failure_ttl_is_never_shorter_than_the_read_deadline(self) -> None:
        # The entry is stamped with the time the read STARTED, so a shorter one
        # would already be lapsed the moment a timeout recorded it, and a hung
        # node would cost EVERY send the full deadline.
        wallet = Wallet(lambda _d: None, ttl_ms=60_000, read_timeout_ms=20_000)  # type: ignore[arg-type]
        assert wallet._failure_ttl_ms == 20_000

    async def test_the_failure_ttl_never_outlives_the_success_ttl(self) -> None:
        wallet = Wallet(lambda _d: None, ttl_ms=1_000, read_timeout_ms=2_000)  # type: ignore[arg-type]
        assert wallet._failure_ttl_ms == 1_000


class TestAttachmentsFor:
    async def test_empty_when_nothing_covers_the_message(self) -> None:
        # Most DIDComm traffic — discovery, trust-ping, problem reports — rides
        # the node's allow rules with no grant at all. Not an error.
        async def reader(_did: str) -> list[dict[str, Any]]:
            return []

        wallet = Wallet(reader)
        assert await wallet.attachments_for(
            "did:web:alice",
            recipients=["did:web:bob"],
            type_uri="https://didcomm.org/trust-ping/2.0/ping",
        ) == []

    async def test_a_read_failure_surfaces_rather_than_reading_as_an_empty_wallet(
        self,
    ) -> None:
        async def reader(_did: str) -> list[dict[str, Any]]:
            raise RuntimeError("nope")

        wallet = Wallet(reader)
        with pytest.raises(RuntimeError):
            await wallet.attachments_for(
                "did:web:alice", recipients=["did:web:bob"], type_uri="p/t"
            )

    async def test_the_tool_name_comes_from_the_body(self) -> None:
        wanted = grant_record(cred_id="urn:uuid:wanted", sig="w", tools=["send_email"])
        others = [
            grant_record(cred_id=f"urn:uuid:o{i}", sig=f"o{i}", tools=["other"])
            for i in range(MAX_ATTACHED)
        ]

        async def reader(_did: str) -> list[dict[str, Any]]:
            return [*others, wanted]

        wallet = Wallet(reader)
        atts = await wallet.attachments_for(
            "did:web:alice",
            recipients=["did:web:bob"],
            type_uri="https://layr8.io/protocols/mcp/1.0/tools-call",
            body={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "send_email"}},
        )
        assert atts[0].id == "urn:uuid:wanted"


class TestRestCredentialReader:
    """The REST shape, checked against a real HTTP server rather than a mock's beliefs."""

    async def _serve(self, handler) -> Any:
        app = web.Application()
        app.router.add_get("/api/v1/credentials", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        return runner, port

    async def test_reads_the_holder_did_query_and_the_credentials_key(self) -> None:
        captured: dict[str, Any] = {}

        async def handler(request: web.Request) -> web.Response:
            captured["holder_did"] = request.query.get("holder_did")
            captured["api_key"] = request.headers.get("x-api-key")
            return web.json_response({"credentials": [grant_record()]})

        runner, port = await self._serve(handler)
        rest = RestClient(f"http://127.0.0.1:{port}", "test-api-key")
        try:
            records = await rest_credential_reader(rest, 2_000)("did:web:alice.localhost")
            assert len(records) == 1
            assert captured["holder_did"] == "did:web:alice.localhost"
            assert captured["api_key"] == "test-api-key"
        finally:
            await rest.close()
            await runner.cleanup()

    async def test_a_bare_list_response_is_accepted_too(self) -> None:
        async def handler(_request: web.Request) -> web.Response:
            return web.json_response([grant_record()])

        runner, port = await self._serve(handler)
        rest = RestClient(f"http://127.0.0.1:{port}", "k")
        try:
            assert len(await rest_credential_reader(rest, 2_000)("did:web:alice")) == 1
        finally:
            await rest.close()
            await runner.cleanup()

    async def test_a_node_that_goes_quiet_costs_one_deadline(self) -> None:
        # The read sits in front of every send; an unbounded one stalls the send
        # itself.
        import asyncio

        # Accept the connection and say nothing — what a hung node looks like
        # from here. Released in `finally` so teardown does not wait it out.
        release = asyncio.Event()

        async def handler(_request: web.Request) -> web.Response:
            await release.wait()
            return web.json_response({"credentials": []})

        runner, port = await self._serve(handler)
        rest = RestClient(f"http://127.0.0.1:{port}", "k")
        try:
            loop = asyncio.get_running_loop()
            started = loop.time()
            with pytest.raises(asyncio.TimeoutError):
                await rest_credential_reader(rest, 150)("did:web:alice")
            assert loop.time() - started < 5
        finally:
            release.set()
            await rest.close()
            await runner.cleanup()
