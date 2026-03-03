"""Internal REST client for the Layr8 cloud-node HTTP API."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp


def rest_url_from_websocket(ws_url: str) -> str:
    """Derive the HTTP base URL from a WebSocket URL.

    ``ws://`` becomes ``http://``, ``wss://`` becomes ``https://``.
    The path, query, and fragment are stripped so only
    ``scheme://host[:port]`` remains.

    Examples::

        ws://localhost:4000/plugin_socket/websocket  -> http://localhost:4000
        wss://node.layr8.cloud/plugin_socket/websocket -> https://node.layr8.cloud
    """
    parsed = urlparse(ws_url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    # Reconstruct with only scheme and netloc (host:port)
    return urlunparse((scheme, parsed.netloc, "", "", "", ""))


class RESTError(Exception):
    """Error from the cloud-node REST API."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"REST API error {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class RestClient:
    """Internal HTTP client for cloud-node REST API.

    Handles JSON serialization, ``x-api-key`` auth, and error parsing.
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON POST request and return the decoded response."""
        session = self._get_session()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["x-api-key"] = self._api_key

        async with session.post(
            self._base_url + path,
            data=json.dumps(body),
            headers=headers,
        ) as resp:
            return await self._handle_response(resp)

    async def get(self, path: str) -> dict[str, Any]:
        """Send a GET request and return the decoded response."""
        session = self._get_session()
        headers: dict[str, str] = {}
        if self._api_key:
            headers["x-api-key"] = self._api_key

        async with session.get(
            self._base_url + path,
            headers=headers,
        ) as resp:
            return await self._handle_response(resp)

    async def _handle_response(self, resp: aiohttp.ClientResponse) -> dict[str, Any]:
        """Read the response body, raise on errors, and decode JSON."""
        body_bytes = await resp.read()

        if resp.status >= 400:
            raise _parse_rest_error(resp.status, body_bytes)

        if body_bytes:
            return json.loads(body_bytes)  # type: ignore[no-any-return]
        return {}

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None


def _parse_rest_error(status_code: int, body: bytes) -> RESTError:
    """Parse an error response body into a RESTError."""
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and parsed.get("error"):
            return RESTError(status_code, parsed["error"])
    except (json.JSONDecodeError, KeyError):
        pass
    return RESTError(status_code, body.decode("utf-8", errors="replace"))
