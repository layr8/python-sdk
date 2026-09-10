"""
`lastmod_time` arrives in two forms, and an attachment nobody can read must
not cost the reader the message.

A cloud-node answers a refused call with an `e.m.authz.denied` problem report
that carries a decision attachment. The attachment's `lastmod_time` is a hint
about that attachment, nothing more — DIDComm v2 states no type for it, so both
epoch seconds and an RFC 3339 string are legal on the wire and both have been
sent. This SDK annotated the field `int` while converting nothing, so a type
checker approved arithmetic on a value that was in fact a `str`.

The header one level up is the same argument at a larger scale: an
`attachments` header this SDK cannot decode used to raise out of
`parse_didcomm`, the client reported a parse failure, and the message was
dropped before routing. A caller waiting on `request()` then heard nothing
until it timed out. Being refused and being ignored are different events.
"""

from __future__ import annotations

import asyncio
from typing import Any, get_args, get_type_hints

import pytest

from layr8 import Client, Config, Message, ProblemReportError, SDKError
from layr8.message import Attachment, AttachmentData, marshal_didcomm, parse_didcomm

from .test_client import MockPhoenixServer, mock_server, ws_url  # noqa: F401 — fixture

ISO_LASTMOD = "2026-09-10T10:16:00.000000Z"
EPOCH_LASTMOD = 1789035360


def _denial(lastmod: Any, *, present: bool = True, data: Any = None) -> dict[str, Any]:
    """An `e.m.authz.denied` problem report carrying one decision attachment."""
    att: dict[str, Any] = {
        "id": "decision",
        "media_type": "application/json",
        "data": {"json": {"allowed": False}} if data is None else data,
    }
    if present:
        att["lastmod_time"] = lastmod
    return {
        "plaintext": {
            "id": "err-1",
            "type": "https://didcomm.org/report-problem/2.0/problem-report",
            "from": "did:web:node",
            "to": ["did:web:alice"],
            "thid": "req-1",
            "body": {"code": "e.m.authz.denied", "comment": "no grant"},
            "attachments": [att],
        }
    }


class TestLastmodTimeForms:
    def test_reads_integer_epoch_seconds(self) -> None:
        msg = parse_didcomm(_denial(EPOCH_LASTMOD))
        assert msg.attachments is not None
        assert msg.attachments[0].lastmod_time == EPOCH_LASTMOD

    def test_reads_an_rfc_3339_string(self) -> None:
        """The form that shipped on the wire before senders were pinned to an integer."""
        msg = parse_didcomm(_denial(ISO_LASTMOD))
        assert msg.attachments is not None
        assert msg.attachments[0].lastmod_time == ISO_LASTMOD

    def test_absent_integer_and_string_are_three_distinct_values(self) -> None:
        """
        The point of the widening. Asserting only two of the three passes while
        the defect is present: a reader that coalesces an unrecognised hint into
        None reports "the sender sent no hint" for a hint that was sent.
        """
        absent = parse_didcomm(_denial(None, present=False)).attachments[0].lastmod_time
        as_int = parse_didcomm(_denial(EPOCH_LASTMOD)).attachments[0].lastmod_time
        as_str = parse_didcomm(_denial(ISO_LASTMOD)).attachments[0].lastmod_time

        assert absent is None
        assert as_int == EPOCH_LASTMOD and isinstance(as_int, int)
        assert as_str == ISO_LASTMOD and isinstance(as_str, str)
        assert absent != as_int
        assert absent != as_str
        assert as_int != as_str

    def test_marshal_passes_both_forms_through_unchanged(self) -> None:
        """
        This SDK does not interpret the hint, so relaying a message never
        rewrites a value a peer chose.
        """
        for value in (EPOCH_LASTMOD, ISO_LASTMOD):
            msg = Message(
                id="m", type="t", from_="did:web:alice", to=["did:web:bob"], body={},
                attachments=[Attachment(id="a", lastmod_time=value,
                                        data=AttachmentData(base64="aGk="))],
            )
            assert marshal_didcomm(msg)["attachments"][0]["lastmod_time"] == value


    def test_the_annotation_itself_admits_both_forms(self) -> None:
        """
        The widening is a promise to a type checker, and nothing else in this
        repo runs one: Python does not enforce an annotation at runtime, so
        every other test here passes with the field annotated `int`. Without
        this assertion the field could be narrowed back and CI would stay
        green, which is how it came to say `int` in the first place.
        """
        annotation = get_type_hints(Attachment)["lastmod_time"]
        admitted = set(get_args(annotation))
        assert int in admitted, f"lastmod_time must admit int, got {annotation}"
        assert str in admitted, (
            "lastmod_time must admit str: DIDComm v2 states no type for the "
            f"field and a sender may send a timestamp string. Got {annotation}"
        )
        assert type(None) in admitted, f"lastmod_time is optional, got {annotation}"


class TestAttachmentsHeaderIsReadOrNotRead:
    def test_no_header_reads_as_no_attachments(self) -> None:
        msg = parse_didcomm({"plaintext": {"id": "m", "type": "t", "body": {}}})
        assert msg.attachments == []
        assert msg.attachments_unread is None

    @pytest.mark.parametrize(
        "header,fragment",
        [
            ({"not": "a list"}, "expected a list"),
            (["a string, not an object"], "expected an object"),
            ([{"id": "a", "data": "not an object"}], "expected an object"),
        ],
    )
    def test_an_undecodable_header_is_not_read_and_is_not_empty(
        self, header: Any, fragment: str
    ) -> None:
        """
        `None` and `[]` are different answers. Returning `[]` would report
        "this message carried no attachments", which nobody measured.
        """
        msg = parse_didcomm(
            {"plaintext": {"id": "m", "type": "t", "body": {}, "attachments": header}}
        )
        assert msg.attachments is None
        assert msg.attachments != []
        assert msg.attachments_unread is not None
        assert fragment in msg.attachments_unread

    def test_the_three_header_states_are_pairwise_distinct(self) -> None:
        read = parse_didcomm(_denial(EPOCH_LASTMOD))
        none_carried = parse_didcomm({"plaintext": {"id": "m", "type": "t", "body": {}}})
        unread = parse_didcomm(
            {"plaintext": {"id": "m", "type": "t", "body": {}, "attachments": "?"}}
        )

        assert (read.attachments, read.attachments_unread) != (
            none_carried.attachments,
            none_carried.attachments_unread,
        )
        assert (none_carried.attachments, none_carried.attachments_unread) != (
            unread.attachments,
            unread.attachments_unread,
        )
        assert (read.attachments, read.attachments_unread) != (
            unread.attachments,
            unread.attachments_unread,
        )


class TestADenialSurvivesItsAttachments:
    """
    The reported symptom, at the seam the caller actually uses. `request()`
    must report the denial, not time out, whatever the decision attachment
    looks like.
    """

    @staticmethod
    def _serve(mock_server: MockPhoenixServer, denial: dict[str, Any]) -> None:
        def handler(msg: dict[str, Any]) -> None:
            if msg["event"] == "phx_join":
                asyncio.ensure_future(mock_server.send_to_client(
                    msg["ref"], msg["ref"], msg["topic"],
                    "phx_reply", {"status": "ok", "response": {}}))
                return
            if msg.get("ref"):
                asyncio.ensure_future(mock_server.send_to_client(
                    None, msg["ref"], msg["topic"],
                    "phx_reply", {"status": "ok", "response": {}}))
            if msg["event"] == "message":
                payload = dict(denial)
                payload["plaintext"] = dict(denial["plaintext"])
                payload["plaintext"]["thid"] = msg["payload"].get("thid", "")
                asyncio.ensure_future(mock_server.send_to_client(
                    None, None, "plugins:did:web:alice", "message", payload))

        mock_server.on_msg = handler

    async def _request(self, mock_server: MockPhoenixServer, denial: dict[str, Any],
                       errors: list[SDKError]) -> ProblemReportError:
        self._serve(mock_server, denial)
        client = Client(
            Config(node_url=ws_url(mock_server), api_key="k", agent_did="did:web:alice"),
            errors.append,
        )
        await client.connect()
        try:
            with pytest.raises(ProblemReportError) as caught:
                await client.request(
                    Message(type="https://layr8.io/protocols/echo/1.0/request",
                            to=["did:web:bob"], body={"message": "hello"}),
                    timeout=5.0,
                )
            return caught.value
        finally:
            await client.close()

    async def test_string_lastmod_reaches_the_waiter(
        self, mock_server: MockPhoenixServer
    ) -> None:
        errors: list[SDKError] = []
        err = await self._request(mock_server, _denial(ISO_LASTMOD), errors)
        assert err.code == "e.m.authz.denied"
        assert errors == []

    async def test_an_undecodable_attachment_still_reaches_the_waiter(
        self, mock_server: MockPhoenixServer
    ) -> None:
        """
        Before the second pass this raised out of `parse_didcomm`, the client
        reported a parse failure and dropped the message, and `request()` sat
        here until its own timeout. The denial has to arrive; the attachment
        it could not decode is reported as unread, not as absent.
        """
        errors: list[SDKError] = []
        err = await self._request(
            mock_server, _denial(EPOCH_LASTMOD, data="not an object"), errors
        )
        assert err.code == "e.m.authz.denied"
        assert errors == []
