"""
Space watch — poll, diff and notify on "does my MCP tool surface still look the
same".

Cross-language contract: ``contracts/sdk-space-watch.md``. ``@layr8/sdk``'s
``SpaceWatcher`` (``src/space-watch.ts``) and the ``layr8`` hex package's
``Layr8.SpaceWatcher`` are the same abstraction in their languages; all three
exist so a caller sees a change at the same latency regardless of which SDK it
is built on.

Two independent signals, both **polled** — nothing on the wire tells an SDK
"your wallet changed" or "a resource came up", and that absence is the reason
this exists at all: since it is on us to notice, everyone should notice on the
same terms.

- **Wallet** — the caller's held VG/credential set. A grant minted or revoked in
  the portal changes this. Polled every 15s by default.
- **Resources** — the Space directory's live MCP Instance cards. An mcp-pod
  registering or losing a directory card changes this. Polled every 60s.

What a "change" MEANS to do about it stays entirely a consumer decision; this
module owns poll, diff and debounce and nothing else. It never inspects the
shape of what it fetched — the default signature works when the fetched value is
already a list of stable ids, and a caller whose value is richer passes its own
reducer. The ORIGINAL fetched value is what reaches the callback, so full
fidelity survives.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal

DEFAULT_WALLET_POLL_MS = 15_000
DEFAULT_RESOURCE_POLL_MS = 60_000

SpaceWatchSignal = Literal["wallet", "resources"]


def order_independent_signature(items: Any) -> str:
    """Sorted, deduped, comma-joined identity of a set of ids."""
    return ",".join(sorted({str(i) for i in items}))


def _default_signature(value: Any) -> str:
    if isinstance(value, (list, tuple, set, frozenset)):
        return order_independent_signature(value)
    return str(value)


def accepts_resource_poll(is_empty: bool, had_resources: bool, empty_streak: int) -> bool:
    """Take this resource poll, or ride out a possibly-transient empty result?

    A directory answering with nothing is not an error, but it is just as likely
    to be a momentary blip (a keepalive miss evicting a card that comes straight
    back) as a real teardown, and acting on it strips every resource-derived tool
    from every live session. Anything non-empty applies at once; so does an empty
    result when there was nothing to lose. Ported from the broker's
    ``acceptsDiscovery``.
    """
    return (not is_empty) or (not had_resources) or empty_streak >= 2


class SpaceWatcher:
    """Watches a wallet and a resource set on independent intervals.

    Both fetches are async callables. Both callbacks receive the freshly fetched
    value, and neither fires on the first successful poll of its signal — that
    seeds the baseline silently, because a cold start is not a change.

    A fetch error never wipes state: it goes to *on_error* (if given) and the
    last-accepted signature is retained for the next poll. A transient wallet-read
    or directory failure must not read as "everything disappeared."

    Resources debounce an empty result; the wallet does not. A wallet answering
    "nothing held" is a real answer, not a blip, and callers must be able to
    trust it immediately.
    """

    def __init__(
        self,
        *,
        fetch_wallet: Callable[[], Awaitable[Any]],
        fetch_resources: Callable[[], Awaitable[Any]],
        on_wallet_change: Callable[[Any], None] | None = None,
        on_resources_change: Callable[[Any], None] | None = None,
        wallet_signature: Callable[[Any], str] | None = None,
        resource_signature: Callable[[Any], str] | None = None,
        on_error: Callable[[SpaceWatchSignal, BaseException], None] | None = None,
        wallet_poll_ms: float = DEFAULT_WALLET_POLL_MS,
        resource_poll_ms: float = DEFAULT_RESOURCE_POLL_MS,
    ) -> None:
        self._fetch_wallet = fetch_wallet
        self._fetch_resources = fetch_resources
        self._on_wallet_change = on_wallet_change
        self._on_resources_change = on_resources_change
        self._wallet_sig = wallet_signature or _default_signature
        self._resource_sig = resource_signature or _default_signature
        self._on_error = on_error
        self._wallet_poll_ms = wallet_poll_ms
        self._resource_poll_ms = resource_poll_ms

        self._last_wallet_sig: str | None = None
        self._last_resource_sig: str | None = None
        self._resource_empty_streak = 0

        self._tasks: list[asyncio.Task[None]] = []
        self._started = False

    async def start(self) -> None:
        """Seed both baselines immediately, then poll each on its own interval."""
        if self._started:
            return
        self._started = True

        await asyncio.gather(self.refresh_wallet(), self.refresh_resources())

        self._tasks = [
            asyncio.ensure_future(self._loop(self._wallet_poll_ms, self._wallet_tick)),
            asyncio.ensure_future(self._loop(self._resource_poll_ms, self._resource_tick)),
        ]

    async def stop(self) -> None:
        """Stop polling. Safe to call when never started."""
        if not self._started:
            return
        self._started = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []

    async def refresh_wallet(self) -> None:
        """Force an immediate out-of-cycle wallet check, e.g. after minting a grant."""
        await self._wallet_tick()

    async def refresh_resources(self) -> None:
        """Force an immediate out-of-cycle resource check."""
        await self._resource_tick()

    async def _loop(self, interval_ms: float, tick: Callable[[], Awaitable[None]]) -> None:
        while self._started:
            await asyncio.sleep(interval_ms / 1000)
            if not self._started:
                return
            await tick()

    async def _wallet_tick(self) -> None:
        try:
            value = await self._fetch_wallet()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._on_error:
                self._on_error("wallet", exc)
            return  # retain last-accepted signature; retry next poll

        sig = self._wallet_sig(value)
        is_first = self._last_wallet_sig is None
        if not is_first and sig != self._last_wallet_sig and self._on_wallet_change:
            self._on_wallet_change(value)
        self._last_wallet_sig = sig  # wallet never debounces empty

    async def _resource_tick(self) -> None:
        try:
            value = await self._fetch_resources()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._on_error:
                self._on_error("resources", exc)
            return  # retain last-accepted signature; retry next poll

        sig = self._resource_sig(value)
        is_empty = sig == ""
        self._resource_empty_streak = self._resource_empty_streak + 1 if is_empty else 0
        had_resources = self._last_resource_sig not in (None, "")

        if not accepts_resource_poll(is_empty, had_resources, self._resource_empty_streak):
            return  # ride out one empty blip; last-accepted signature untouched

        is_first = self._last_resource_sig is None
        if not is_first and sig == self._last_resource_sig:
            return
        self._last_resource_sig = sig
        if not is_first and self._on_resources_change:
            self._on_resources_change(value)
