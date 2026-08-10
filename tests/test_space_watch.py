"""Tests for layr8.space_watch — the poll/diff/debounce contract.

These pin the behaviour every Layr8 SDK's watcher shares, so the Python side
cannot drift from it silently.
"""

from __future__ import annotations

import asyncio
from typing import Any

from layr8.space_watch import (
    SpaceWatcher,
    accepts_resource_poll,
    order_independent_signature,
)


class Signal:
    """A fetch function whose answers are scripted, one per call."""

    def __init__(self, *answers: Any) -> None:
        self._answers = list(answers)
        self.calls = 0

    async def __call__(self) -> Any:
        self.calls += 1
        answer = self._answers[min(self.calls - 1, len(self._answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


def watcher(wallet: Signal, resources: Signal, **kwargs: Any):
    changes: dict[str, list[Any]] = {"wallet": [], "resources": [], "errors": []}
    w = SpaceWatcher(
        fetch_wallet=wallet,
        fetch_resources=resources,
        on_wallet_change=changes["wallet"].append,
        on_resources_change=changes["resources"].append,
        on_error=lambda signal, err: changes["errors"].append((signal, err)),
        **kwargs,
    )
    return w, changes


class TestSignature:
    def test_order_does_not_matter(self) -> None:
        assert order_independent_signature(["b", "a"]) == order_independent_signature(
            ["a", "b"]
        )

    def test_duplicates_do_not_matter(self) -> None:
        assert order_independent_signature(["a", "a", "b"]) == order_independent_signature(
            ["a", "b"]
        )

    def test_empty_is_the_empty_string(self) -> None:
        """That's what drives the resource empty-result debounce."""
        assert order_independent_signature([]) == ""


class TestAcceptsResourcePoll:
    def test_anything_non_empty_applies_at_once(self) -> None:
        assert accepts_resource_poll(is_empty=False, had_resources=True, empty_streak=0)

    def test_empty_applies_when_there_was_nothing_to_lose(self) -> None:
        assert accepts_resource_poll(is_empty=True, had_resources=False, empty_streak=1)

    def test_the_first_empty_after_a_non_empty_baseline_is_ridden_out(self) -> None:
        # A directory answering with nothing is just as likely to be a keepalive
        # blip as a real teardown, and acting on it strips every
        # resource-derived tool from every live session.
        assert not accepts_resource_poll(is_empty=True, had_resources=True, empty_streak=1)

    def test_two_consecutive_empties_are_believed(self) -> None:
        assert accepts_resource_poll(is_empty=True, had_resources=True, empty_streak=2)


class TestWalletSignal:
    async def test_the_first_poll_seeds_the_baseline_silently(self) -> None:
        """A cold start is not a change."""
        w, changes = watcher(Signal(["cred-1"]), Signal([]))
        await w.refresh_wallet()
        assert changes["wallet"] == []

    async def test_a_change_notifies_with_the_fresh_value(self) -> None:
        w, changes = watcher(Signal(["cred-1"], ["cred-1", "cred-2"]), Signal([]))
        await w.refresh_wallet()
        await w.refresh_wallet()
        assert changes["wallet"] == [["cred-1", "cred-2"]]

    async def test_the_same_set_in_a_different_order_is_not_a_change(self) -> None:
        w, changes = watcher(Signal(["a", "b"], ["b", "a"]), Signal([]))
        await w.refresh_wallet()
        await w.refresh_wallet()
        assert changes["wallet"] == []

    async def test_an_empty_wallet_is_a_real_answer_and_never_debounces(self) -> None:
        # A wallet answering "nothing held" is a different failure shape from a
        # directory blip, and callers must be able to trust it immediately.
        w, changes = watcher(Signal(["cred-1"], []), Signal([]))
        await w.refresh_wallet()
        await w.refresh_wallet()
        assert changes["wallet"] == [[]]

    async def test_a_fetch_error_never_wipes_the_retained_signature(self) -> None:
        # A transient wallet-read failure must not read as "everything
        # disappeared."
        w, changes = watcher(
            Signal(["cred-1"], RuntimeError("boom"), ["cred-1"]), Signal([])
        )
        await w.refresh_wallet()
        await w.refresh_wallet()
        await w.refresh_wallet()

        assert changes["wallet"] == []
        assert [s for s, _ in changes["errors"]] == ["wallet"]


class TestResourceSignal:
    async def test_the_first_poll_seeds_the_baseline_silently(self) -> None:
        w, changes = watcher(Signal([]), Signal(["did:web:pod-a"]))
        await w.refresh_resources()
        assert changes["resources"] == []

    async def test_growth_applies_immediately(self) -> None:
        w, changes = watcher(Signal([]), Signal(["a"], ["a", "b"]))
        await w.refresh_resources()
        await w.refresh_resources()
        assert changes["resources"] == [["a", "b"]]

    async def test_shrinking_to_a_still_non_empty_set_applies_immediately(self) -> None:
        w, changes = watcher(Signal([]), Signal(["a", "b"], ["a"]))
        await w.refresh_resources()
        await w.refresh_resources()
        assert changes["resources"] == [["a"]]

    async def test_one_empty_poll_is_ridden_out(self) -> None:
        w, changes = watcher(Signal([]), Signal(["a"], [], ["a"]))
        await w.refresh_resources()
        await w.refresh_resources()  # empty — not believed yet
        assert changes["resources"] == []
        await w.refresh_resources()  # came straight back; nothing was notified
        assert changes["resources"] == []

    async def test_two_consecutive_empties_are_believed(self) -> None:
        w, changes = watcher(Signal([]), Signal(["a"], [], []))
        await w.refresh_resources()
        await w.refresh_resources()
        await w.refresh_resources()
        assert changes["resources"] == [[]]

    async def test_a_fetch_error_never_wipes_the_retained_signature(self) -> None:
        w, changes = watcher(Signal([]), Signal(["a"], RuntimeError("directory down"), ["a"]))
        await w.refresh_resources()
        await w.refresh_resources()
        await w.refresh_resources()

        assert changes["resources"] == []
        assert [s for s, _ in changes["errors"]] == ["resources"]

    async def test_an_error_does_not_count_toward_the_empty_streak(self) -> None:
        # An error is not an answer. Counting it would let one failed poll plus
        # one real empty tear down every resource-derived tool.
        w, changes = watcher(Signal([]), Signal(["a"], RuntimeError("x"), []))
        await w.refresh_resources()
        await w.refresh_resources()
        await w.refresh_resources()
        assert changes["resources"] == []


class TestCustomSignature:
    async def test_the_callback_still_receives_the_original_value(self) -> None:
        # The watcher only ever compares signature strings; full fidelity has to
        # survive to the callback, which may need the whole object.
        first = [{"did": "did:web:pod-a", "tools": 3}]
        second = [{"did": "did:web:pod-b", "tools": 7}]

        changes: list[Any] = []
        w = SpaceWatcher(
            fetch_wallet=Signal([]),
            fetch_resources=Signal(first, second),
            on_resources_change=changes.append,
            resource_signature=lambda rs: order_independent_signature(
                [r["did"] for r in rs]
            ),
        )
        await w.refresh_resources()
        await w.refresh_resources()

        assert changes == [second]


class TestLifecycle:
    async def test_start_seeds_both_baselines_without_notifying(self) -> None:
        wallet, resources = Signal(["a"]), Signal(["b"])
        w, changes = watcher(wallet, resources, wallet_poll_ms=50, resource_poll_ms=50)

        await w.start()
        try:
            assert wallet.calls == 1
            assert resources.calls == 1
            assert changes["wallet"] == [] and changes["resources"] == []
        finally:
            await w.stop()

    async def test_each_signal_polls_on_its_own_interval(self) -> None:
        wallet, resources = Signal(["a"]), Signal(["b"])
        w, _ = watcher(wallet, resources, wallet_poll_ms=20, resource_poll_ms=10_000)

        await w.start()
        try:
            await asyncio.sleep(0.11)
            assert wallet.calls > 2, "wallet should have polled several times"
            assert resources.calls == 1, "resources should still be on its baseline"
        finally:
            await w.stop()

    async def test_start_is_idempotent(self) -> None:
        wallet, resources = Signal(["a"]), Signal(["b"])
        w, _ = watcher(wallet, resources, wallet_poll_ms=10_000, resource_poll_ms=10_000)

        await w.start()
        try:
            await w.start()
            assert wallet.calls == 1
        finally:
            await w.stop()

    async def test_stop_ends_the_polling(self) -> None:
        wallet, resources = Signal(["a"]), Signal(["b"])
        w, _ = watcher(wallet, resources, wallet_poll_ms=20, resource_poll_ms=20)

        await w.start()
        await w.stop()
        after_stop = wallet.calls
        await asyncio.sleep(0.08)
        assert wallet.calls == after_stop

    async def test_stop_without_start_is_safe(self) -> None:
        w, _ = watcher(Signal([]), Signal([]))
        await w.stop()
