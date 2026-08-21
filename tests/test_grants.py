"""Client-level Verifiable Grant attachment: what actually reaches the wire.

The mock Phoenix server and the stub node cannot share a port (the REST base is
derived from the WebSocket URL), so the wallet is built over a stub reader and
installed on the client. Everything downstream of that — selection, the
attachment shape, the marshalled envelope, the unattached/denial correlation —
is the real code path. The REST shape itself is checked against a real HTTP
server in ``test_wallet.py``.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest

from layr8 import (
    Attachment,
    AttachmentData,
    Client,
    Config,
    GrantMissInfo,
    Message,
    SDKError,
    identity_attachment,
    is_identity_attachment,
)
from layr8.wallet import Wallet

from .test_client import MockPhoenixServer, mock_server, ws_url  # noqa: F401
from .test_wallet import grant_record, jwt


def _discard_errors(err: SDKError) -> None:
    pass


def make_client(
    server: MockPhoenixServer,
    *,
    records: list[dict[str, Any]] | Exception | None = None,
    on_grant_miss: Any = None,
    attach_grants: bool = True,
) -> tuple[Client, dict[str, int]]:
    """A client whose wallet reads *records* instead of talking to a node."""
    calls = {"reads": 0}

    async def reader(_did: str) -> list[dict[str, Any]]:
        calls["reads"] += 1
        if isinstance(records, Exception):
            raise records
        return records or []

    client = Client(
        Config(
            node_url=ws_url(server),
            api_key="test-api-key",
            agent_did="did:web:alice.localhost",
            attach_grants=attach_grants,
            on_grant_miss=on_grant_miss,
        ),
        _discard_errors,
    )
    if attach_grants:
        client._wallet = Wallet(reader)
    return client, calls


def sent_messages(server: MockPhoenixServer) -> list[dict[str, Any]]:
    return [r["payload"] for r in server.get_received() if r["event"] == "message"]


COVERING = [{"protocol": "*", "messageTypes": ["*"]}]


def a_message(**kwargs: Any) -> Message:
    defaults: dict[str, Any] = {
        "type": "https://layr8.io/protocols/mcp/1.0/tools-call",
        "to": ["did:web:bob.localhost"],
        "body": {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "send"}},
    }
    return Message(**{**defaults, **kwargs})


class TestAttachmentOnTheWire:
    async def test_a_covering_grant_rides_out_as_application_vc_jwt(
        self, mock_server: MockPhoenixServer
    ) -> None:
        rec = grant_record(scope=COVERING)
        client, _ = make_client(mock_server, records=[rec])

        await client.connect()
        try:
            await client.send(a_message())
        finally:
            await client.close()

        (payload,) = sent_messages(mock_server)
        (att,) = payload["attachments"]
        # media_type is the ONLY field the node's extractor filters on, by exact
        # string equality; anything else is dropped silently and denied
        # identically to attaching nothing.
        assert att["media_type"] == "application/vc+jwt"
        assert att["data"] == {"jws": rec["credential_jwt"]}

    async def test_nothing_covering_means_no_attachments_and_the_send_still_happens(
        self, mock_server: MockPhoenixServer
    ) -> None:
        client, _ = make_client(mock_server, records=[])

        await client.connect()
        try:
            await client.send(a_message())
        finally:
            await client.close()

        (payload,) = sent_messages(mock_server)
        assert "attachments" not in payload

    async def test_caller_supplied_attachments_are_never_displaced(
        self, mock_server: MockPhoenixServer
    ) -> None:
        # Someone passing their own has a reason, and silently overriding it
        # would be the second confusing thing to happen to that message.
        client, calls = make_client(mock_server, records=[grant_record(scope=COVERING)])
        # A GRANT of the caller's own. The fixture used to be an undecodable
        # three-segment string, which now reads as an identity credential (no
        # scope) and so exercises the narrowing below instead of this rule.
        mine = Attachment(
            id="mine",
            media_type="application/vc+jwt",
            data=AttachmentData(jws=grant_record(scope=COVERING, cred_id="mine")["credential_jwt"]),
        )

        await client.connect()
        try:
            await client.send(a_message(attachments=[mine]))
        finally:
            await client.close()

        (payload,) = sent_messages(mock_server)
        assert [a["id"] for a in payload["attachments"]] == ["mine"]
        assert calls["reads"] == 0

    async def test_attach_grants_false_reads_nothing_at_all(
        self, mock_server: MockPhoenixServer
    ) -> None:
        client, calls = make_client(mock_server, attach_grants=False)

        await client.connect()
        try:
            await client.send(a_message())
        finally:
            await client.close()

        assert client._wallet is None
        assert calls["reads"] == 0

    async def test_a_request_carries_its_grants_too(
        self, mock_server: MockPhoenixServer
    ) -> None:
        rec = grant_record(scope=COVERING)
        client, _ = make_client(mock_server, records=[rec])

        await client.connect()
        try:
            task = asyncio.ensure_future(client.request(a_message(), timeout=2.0))
            await asyncio.sleep(0.05)
            (payload,) = sent_messages(mock_server)
            assert payload["attachments"][0]["media_type"] == "application/vc+jwt"
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        finally:
            await client.close()

    async def test_a_handler_reply_carries_its_grants_too(
        self, mock_server: MockPhoenixServer
    ) -> None:
        # A reply is a message the node authorizes exactly like the request that
        # prompted it.
        rec = grant_record(scope=COVERING)
        client, _ = make_client(mock_server, records=[rec])

        @client.handle("https://layr8.io/protocols/echo/1.0/request")
        async def echo(msg: Message) -> Message:
            return Message(
                type="https://layr8.io/protocols/echo/1.0/response", body={"text": "pong"}
            )

        await client.connect()
        try:
            mock_server.clear_received()
            await mock_server.send_to_client(
                None,
                None,
                "plugin:did:web:alice.localhost",
                "message",
                {
                    "plaintext": {
                        "id": "inbound-1",
                        "type": "https://layr8.io/protocols/echo/1.0/request",
                        "from": "did:web:bob.localhost",
                        "to": ["did:web:alice.localhost"],
                        "body": {"text": "ping"},
                    }
                },
            )
            await asyncio.sleep(0.2)

            replies = sent_messages(mock_server)
            assert replies, "handler reply never reached the wire"
            assert replies[0]["attachments"][0]["media_type"] == "application/vc+jwt"
        finally:
            await client.close()

    async def test_the_grants_are_read_once_and_cached_across_sends(
        self, mock_server: MockPhoenixServer
    ) -> None:
        client, calls = make_client(mock_server, records=[grant_record(scope=COVERING)])

        await client.connect()
        try:
            await client.send(a_message())
            await client.send(a_message())
            assert calls["reads"] == 1

            # A grant minted seconds ago is invisible until the TTL lapses; an
            # agent that has just been told it was granted something should not
            # have to wait out a timer it cannot see.
            client.refresh_grants()
            await client.send(a_message())
            assert calls["reads"] == 2
        finally:
            await client.close()

    async def test_sends_keep_their_call_order_despite_the_grant_read(
        self, mock_server: MockPhoenixServer
    ) -> None:
        # The read puts an await in front of every send. Agents that emit a
        # sequence without awaiting each call are entitled to their order.
        client, _ = make_client(mock_server, records=[grant_record(scope=COVERING)])

        await client.connect()
        try:
            first = asyncio.ensure_future(client.send(a_message(body={"n": 1})))
            second = asyncio.ensure_future(client.send(a_message(body={"n": 2})))
            await asyncio.gather(first, second)

            assert [p["body"]["n"] for p in sent_messages(mock_server)] == [1, 2]
        finally:
            await client.close()


def identity_jwt(cred_id: str = "urn:uuid:idc-1", sig: str = "identity-sig") -> str:
    """An identity credential: same claims shape as a grant, and NO
    ``credentialSubject.scope``. That absence is the entire discriminator, on
    this side and on the node's."""
    return jwt(
        {
            "id": cred_id,
            "type": ["VerifiableCredential", "EmploymentCredential"],
            "issuer": "did:web:issuer.localhost",
            "credentialSubject": {
                "id": "did:web:alice.localhost",
                "employer": "Example Incorporated",
                "role": "buyer",
            },
        },
        sig,
    )


class TestIdentityCredentialOnTheWire:
    """Boundary test for the sender -> cloud-node identity-credential contract.

    The node routes an attachment on ``credentialSubject.scope`` alone, so the
    two claims this SDK must keep straight are "no scope, so identity" and "has
    a scope, so grant".
    """

    async def test_it_goes_out_exactly_as_the_caller_built_it(
        self, mock_server: MockPhoenixServer
    ) -> None:
        client, _ = make_client(mock_server, records=[])
        raw = identity_jwt()

        await client.connect()
        try:
            await client.send(a_message(attachments=[identity_attachment(raw)]))
        finally:
            await client.close()

        (payload,) = sent_messages(mock_server)
        assert payload["attachments"][0] == {
            "id": "urn:uuid:idc-1",
            "media_type": "application/vc+jwt",
            "data": {"jws": raw},
        }

    async def test_it_does_not_cost_the_message_its_grant(
        self, mock_server: MockPhoenixServer
    ) -> None:
        # The half that would be silently wrong. Under the old rule ANY
        # caller-supplied attachment made the wallet stand aside, so saying who
        # you are meant sending nothing that says what you may do — and the
        # node's denial then read "no grant covers this call", which is exactly
        # the message that sends people looking at their grant configuration.
        rec = grant_record(scope=COVERING)
        client, _ = make_client(mock_server, records=[rec])
        raw = identity_jwt()

        await client.connect()
        try:
            await client.send(a_message(attachments=[identity_attachment(raw)]))
        finally:
            await client.close()

        (payload,) = sent_messages(mock_server)
        # The caller's stays FIRST and unmodified; the wallet's selection follows.
        assert [a["id"] for a in payload["attachments"]] == ["urn:uuid:idc-1", "urn:uuid:grant-1"]
        assert payload["attachments"][1]["data"] == {"jws": rec["credential_jwt"]}

    def test_a_credential_with_a_scope_is_refused(self) -> None:
        # Not a taste call. The node would route it to the policy's
        # `credentials` input, where it can never satisfy a `senderCredentials`
        # requirement, and the denial that follows is byte-for-byte the one for
        # attaching nothing at all. The check is local and exact, so the choice
        # is between raising at the call site and a misroute diagnosed at the
        # far end.
        grant = grant_record(scope=COVERING)["credential_jwt"]
        with pytest.raises(ValueError, match="Verifiable Grant"):
            identity_attachment(grant)

        assert not is_identity_attachment(
            Attachment(media_type="application/vc+jwt", data=AttachmentData(jws=grant))
        )
        assert is_identity_attachment(
            Attachment(media_type="application/vc+jwt", data=AttachmentData(jws=identity_jwt()))
        )

    def test_anything_that_is_not_a_compact_jws_is_refused(self) -> None:
        # The node can verify nothing else, so attaching it only buys a denial
        # that names the wrong problem.
        with pytest.raises(ValueError, match="compact JWS"):
            identity_attachment("not-a-jws")
        with pytest.raises(ValueError, match="compact JWS"):
            identity_attachment("a.b.")

    def test_an_undecodable_attachment_is_not_an_identity_credential(self) -> None:
        # Counting three segments is not reading a credential. Each of these has
        # three of them and decodes to nothing usable, so nothing here can say
        # whether it carries a `credentialSubject.scope`.
        #
        # "I could not read a scope" must not collapse into "there is no scope,
        # so this is identity". Identity is the ONE attachment shape that leaves
        # the wallet running, so that collapse hands a caller who attached
        # garbage the wallet's grants, appended silently, while every other
        # foreign attachment stands the wallet aside. The caller chose nothing
        # and got a disclosure.
        def seg(raw: bytes) -> str:
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        undecodable = [
            "..",
            "a.b.c",
            f"{seg(b'{}')}.{seg(b'not json at all')}.c2ln",
            # Valid JSON, but a scalar: it parses, and then has no
            # `credentialSubject` to read — which looked exactly like a
            # scope-free credential.
            f"{seg(b'{}')}.{seg(b'42')}.c2ln",
        ]

        for raw in undecodable:
            assert not is_identity_attachment(
                Attachment(media_type="application/vc+jwt", data=AttachmentData(jws=raw))
            ), raw
            with pytest.raises(ValueError, match="compact JWS"):
                identity_attachment(raw)

    async def test_identity_mixed_with_a_grant_still_displaces_the_wallet(
        self, mock_server: MockPhoenixServer
    ) -> None:
        # The narrowing is "the caller's attachments are ALL identity
        # credentials", not "at least one of them is". A caller that supplied a
        # grant of its own has said which grant to use, and the wallet appending
        # its own selection behind that would be overriding an explicit choice —
        # the same silent substitution the whole path exists to avoid. Mixing
        # the two is the case where both rules apply at once, and nothing pinned
        # which one wins.
        client, _ = make_client(mock_server, records=[grant_record(scope=COVERING)])
        raw = identity_jwt()
        mine = grant_record(scope=COVERING, cred_id="urn:uuid:mine", sig="mine")

        await client.connect()
        try:
            await client.send(
                a_message(
                    attachments=[
                        identity_attachment(raw),
                        Attachment(
                            id="mine",
                            media_type="application/vc+jwt",
                            data=AttachmentData(jws=mine["credential_jwt"]),
                        ),
                    ]
                )
            )
        finally:
            await client.close()

        (payload,) = sent_messages(mock_server)
        # Both of the caller's survive, in order, and the wallet adds nothing.
        assert [a["id"] for a in payload["attachments"]] == ["urn:uuid:idc-1", "mine"]


class TestGrantMiss:
    async def test_a_read_failure_is_announced_and_does_not_block_the_message(
        self, mock_server: MockPhoenixServer
    ) -> None:
        # The node is the authority on whether this message needed a grant, and
        # most traffic needs none; refusing here on a transient failure would
        # take down calls that were never going to need us.
        misses: list[GrantMissInfo] = []
        client, _ = make_client(
            mock_server, records=RuntimeError("unauthorized"), on_grant_miss=misses.append
        )

        await client.connect()
        try:
            await client.send(a_message())
        finally:
            await client.close()

        assert sent_messages(mock_server), "the send was blocked by a wallet failure"
        assert len(misses) == 1
        assert isinstance(misses[0].error, RuntimeError)
        assert misses[0].to == ["did:web:bob.localhost"]

    async def test_the_cap_is_announced_at_once(
        self, mock_server: MockPhoenixServer
    ) -> None:
        # Unlike "nothing covered it", this is never the normal shape of a
        # message that needs no grant, and it will recur on every send until
        # someone prunes the wallet.
        misses: list[GrantMissInfo] = []
        records = [
            grant_record(scope=COVERING, cred_id=f"urn:uuid:g{i}", sig=f"s{i}")
            for i in range(20)
        ]
        client, _ = make_client(mock_server, records=records, on_grant_miss=misses.append)

        await client.connect()
        try:
            await client.send(a_message())
        finally:
            await client.close()

        assert [m.capped for m in misses] == [{"covering": 20, "attached": 16}]

    async def test_an_unattached_message_alone_says_nothing(
        self, mock_server: MockPhoenixServer
    ) -> None:
        # Discovery, trust-ping and problem reports legitimately need no grant.
        # A diagnostic that fires on every one of them is one nobody reads when
        # it matters.
        misses: list[GrantMissInfo] = []
        client, _ = make_client(mock_server, records=[], on_grant_miss=misses.append)

        await client.connect()
        try:
            for _ in range(5):
                await client.send(a_message())
        finally:
            await client.close()

        assert misses == []

    async def test_a_denial_for_an_unattached_message_is_the_case_it_exists_for(
        self, mock_server: MockPhoenixServer
    ) -> None:
        misses: list[GrantMissInfo] = []
        client, _ = make_client(mock_server, records=[], on_grant_miss=misses.append)

        await client.connect()
        try:
            await client.send(a_message(thread_id="thread-42"))

            # The node's own denial sets `pthid` — to the denied message's
            # `thid` — and sets no `thid` at all, which is why the pthid lookup
            # is the one that matches in production.
            await mock_server.send_to_client(
                None,
                None,
                "plugin:did:web:alice.localhost",
                "message",
                {
                    "plaintext": {
                        "id": "denial-1",
                        "type": "https://didcomm.org/report-problem/2.0/problem-report",
                        "from": "did:web:bob.localhost",
                        "pthid": "thread-42",
                        "body": {
                            "code": "e.m.authz.denied",
                            "comment": "no grant covers this call",
                        },
                    }
                },
            )
            await asyncio.sleep(0.15)
        finally:
            await client.close()

        assert len(misses) == 1
        assert misses[0].denial_code == "e.m.authz.denied"
        assert misses[0].type == "https://layr8.io/protocols/mcp/1.0/tools-call"
        assert misses[0].to == ["did:web:bob.localhost"]

    async def test_a_denial_for_a_message_that_DID_carry_a_grant_says_nothing(
        self, mock_server: MockPhoenixServer
    ) -> None:
        misses: list[GrantMissInfo] = []
        client, _ = make_client(
            mock_server, records=[grant_record(scope=COVERING)], on_grant_miss=misses.append
        )

        await client.connect()
        try:
            await client.send(a_message(thread_id="thread-7"))
            await mock_server.send_to_client(
                None,
                None,
                "plugin:did:web:alice.localhost",
                "message",
                {
                    "plaintext": {
                        "id": "denial-2",
                        "type": "https://didcomm.org/report-problem/2.0/problem-report",
                        "pthid": "thread-7",
                        "body": {"code": "e.m.authz.denied"},
                    }
                },
            )
            await asyncio.sleep(0.15)
        finally:
            await client.close()

        assert misses == []

    async def test_a_non_authz_problem_report_is_not_a_grant_miss(
        self, mock_server: MockPhoenixServer
    ) -> None:
        misses: list[GrantMissInfo] = []
        client, _ = make_client(mock_server, records=[], on_grant_miss=misses.append)

        await client.connect()
        try:
            await client.send(a_message(thread_id="thread-9"))
            await mock_server.send_to_client(
                None,
                None,
                "plugin:did:web:alice.localhost",
                "message",
                {
                    "plaintext": {
                        "id": "problem-1",
                        "type": "https://didcomm.org/report-problem/2.0/problem-report",
                        "pthid": "thread-9",
                        "body": {"code": "e.p.xfer.cant-process"},
                    }
                },
            )
            await asyncio.sleep(0.15)
        finally:
            await client.close()

        assert misses == []

    async def test_a_raising_callback_does_not_break_the_send(
        self, mock_server: MockPhoenixServer
    ) -> None:
        def explode(_info: GrantMissInfo) -> None:
            raise RuntimeError("bad callback")

        client, _ = make_client(
            mock_server, records=RuntimeError("unauthorized"), on_grant_miss=explode
        )

        await client.connect()
        try:
            await client.send(a_message())
        finally:
            await client.close()

        assert sent_messages(mock_server)


class TestWriteOrdering:
    async def test_a_slow_server_ack_does_not_block_the_next_send(
        self, mock_server: MockPhoenixServer
    ) -> None:
        # The grant read is serialized to keep sends in call order, but the lock
        # must NOT cover the channel write: `PhoenixChannel.send` waits up to 15s
        # for the server's ack, and holding the lock across that would give this
        # client head-of-line blocking it never had — one slow ack stalling every
        # other send and every handler reply behind it.
        def join_only(msg: dict[str, Any]) -> None:
            if msg["event"] == "phx_join":
                asyncio.ensure_future(
                    mock_server.send_to_client(
                        msg["ref"], msg["ref"], msg["topic"], "phx_reply",
                        {"status": "ok", "response": {"did": "did:web:alice.localhost"}},
                    )
                )
            # Every other event is left unanswered: a node that took the message
            # and never acked it.

        mock_server.on_msg = join_only
        client, _ = make_client(mock_server, records=[grant_record(scope=COVERING)])

        await client.connect()
        try:
            first = asyncio.ensure_future(client.send(a_message(body={"n": 1})))
            second = asyncio.ensure_future(client.send(a_message(body={"n": 2})))

            # Neither can finish (no ack is coming), but both frames must be on
            # the wire well inside the 15s ack timeout.
            await asyncio.sleep(0.5)
            assert [p["body"]["n"] for p in sent_messages(mock_server)] == [1, 2]

            for task in (first, second):
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            await client.close()
