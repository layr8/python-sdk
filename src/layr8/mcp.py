"""
MCP (Model Context Protocol) over Layr8 DIDComm.

A growing set of Layr8 services (Loom is the first) expose an MCP surface as
DIDComm request/reply: a request of type ``{base}/<method>`` carrying a JSON-RPC
2.0 body, answered by a ``{base}/<method>-result`` message whose body is the
JSON-RPC response. The reply echoes the request's DIDComm ``thid``, so
:meth:`layr8.Client.request` correlates it automatically — this module just
removes the boilerplate (protocol subscription, the ``{base}/…`` type, the
JSON-RPC envelope, and unwrapping ``result`` / raising on ``error``).

Cross-language contract: ``contracts/mcp-over-didcomm.md``. The Node SDK's
``src/mcp.ts`` is the same abstraction.

Usage — ``client.mcp(...)`` must be called BEFORE ``connect()``, like
``handle()``, because it registers the protocol subscription the node needs in
order to deliver replies::

    mcp = client.mcp()                    # default base, registers subscription
    await client.connect()

    loom = mcp.peer(loom_did)
    await loom.initialize()
    tools = await loom.list_tools()
    await loom.call_tool("create_workflow", {"name": name})
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any

from .message import Message

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from .client import Client

#: The default MCP protocol base (``mcp/1.0``).
DEFAULT_MCP_BASE = "https://layr8.io/protocols/mcp/1.0"


class McpError(Exception):
    """Raised when a peer answers a call with a JSON-RPC ``error`` object.

    Distinct from :class:`layr8.ProblemReportError`, which is the DIDComm-level
    failure — including an authorization denial, whose usual cause is a
    Verifiable Grant that never reached the wire rather than one that is
    misconfigured. See :mod:`layr8.wallet` and ``Config.on_grant_miss``.
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"MCP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


def type_for_method(base: str, method: str) -> str:
    """The DIDComm type for an MCP method: ``tools/call`` → ``{base}/tools-call``."""
    return f"{base}/{method.replace('/', '-')}"


class McpPeer:
    """A peer-bound MCP caller, obtained via ``client.mcp().peer(did)``.

    Each :meth:`call` sends one JSON-RPC request and returns its ``result``.
    """

    def __init__(self, client: Client, did: str, base: str) -> None:
        self._client = client
        self.did = did
        self.base = base
        self._ids = itertools.count(1)

    async def call(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: float = 30.0,
    ) -> Any:
        """Call an MCP *method* on the peer and return the JSON-RPC ``result``.

        Raises :class:`McpError` if the peer answers with an ``error``, or
        whatever :meth:`layr8.Client.request` raises (``asyncio.TimeoutError``,
        :class:`layr8.ProblemReportError`).
        """
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
        }
        if params is not None:
            body["params"] = params

        reply = await self._client.request(
            Message(type=type_for_method(self.base, method), to=[self.did], body=body),
            timeout=timeout,
        )

        result = reply.unmarshal_body()
        if not isinstance(result, dict):
            raise McpError(-32_603, f"peer returned a non-JSON-RPC body: {result!r}")
        if "error" in result and isinstance(result["error"], dict):
            err = result["error"]
            raise McpError(err.get("code", -32_603), err.get("message", ""), err.get("data"))
        if "result" not in result:
            # A reply with neither `result` nor `error` is not a JSON-RPC
            # response. Returning `None` would be indistinguishable from a peer
            # that genuinely answered with one.
            raise McpError(-32_603, f"peer returned neither result nor error: {result!r}")
        return result["result"]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> Any:
        """Convenience for MCP ``tools/call``."""
        return await self.call(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=timeout,
        )

    async def list_tools(self, *, timeout: float = 30.0) -> list[dict[str, Any]]:
        """Convenience for MCP ``tools/list``; returns the ``tools`` array."""
        result = await self.call("tools/list", timeout=timeout)
        if isinstance(result, dict):
            tools = result.get("tools")
            if isinstance(tools, list):
                return tools
        return []

    async def initialize(
        self,
        client_info: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> Any:
        """Convenience for MCP ``initialize``."""
        return await self.call(
            "initialize",
            {"clientInfo": client_info or {"name": "layr8-python-sdk"}},
            timeout=timeout,
        )


class McpBinding:
    """A base-bound MCP binding. Call :meth:`peer` to get a caller."""

    def __init__(self, client: Client, base: str) -> None:
        self._client = client
        self.base = base

    def peer(self, did: str) -> McpPeer:
        """A caller bound to *did* on this binding's protocol base."""
        return McpPeer(self._client, did, self.base)
