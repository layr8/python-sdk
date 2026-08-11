# Changelog

All notable changes to `layr8`. Format loosely follows [Keep a Changelog](https://keepachangelog.com/); versioning follows [SemVer](https://semver.org/).

This file starts here. Earlier releases are recorded only in git history.

## [Unreleased]

## [0.2.12] - 2026-08-10

### Added

- **Verifiable Grants are attached to outbound messages** — automatically, on
  every send path (`send`, `request`, and a handler's reply). The cloud-node
  requires a grant for anything its policy does not allow outright, and nothing
  in this SDK attached one: an agent that connected directly sent nothing and
  was denied with "no grant covers this call", a message that reads as "your
  grant is misconfigured" when the truth is "no credential was ever put on the
  wire".

  `layr8.Wallet` reads the holder's credentials from the node, caches them for
  `grant_cache_ms` (default 60s) and selects the covering set with a mirror of
  the node's authorization policy. Caller-supplied attachments are never
  displaced, and a wallet failure never blocks the send.

  New config: `attach_grants` (default `True`, env `LAYR8_ATTACH_GRANTS`),
  `grant_cache_ms`, `grant_read_timeout_ms`, `on_grant_miss`. New API:
  `Client.refresh_grants()`.

- **`on_grant_miss`** and `GrantMissInfo` — told when the node denied a message
  that went out with nothing attached, when the covering set had to be capped
  at 16, or when the grants could not be read at all. It deliberately stays
  quiet on "nothing covered this message" alone: most traffic (discovery,
  trust-ping, problem reports) needs no grant.

- **MCP over DIDComm** — `client.mcp()` returns a binding whose `peer(did)`
  yields a caller with `initialize()`, `list_tools()` and `call_tool()`. It
  handles the protocol subscription, the `tools/call` → `{base}/tools-call`
  type mapping, the JSON-RPC envelope and unwrapping `result`. Must be called
  before `connect()`, like `handle()`. New `McpError` for a JSON-RPC `error`
  from the peer; a DIDComm-level failure (including an authorization denial)
  still raises `ProblemReportError`.

- **`SpaceWatcher`** — the dual-signal poll/diff/notify loop for "does my MCP
  tool surface still look the same", on the semantics every Layr8 SDK shares:
  independent wallet (15s) and resource (60s) intervals, order-independent
  signatures, a first poll that seeds the baseline silently, a fetch error that
  never wipes state, and a two-consecutive-empties debounce on resources but
  never on the wallet.

- **`rest_timeout_ms`** (default 30s, env `LAYR8_REST_TIMEOUT_MS`) — a deadline
  on every REST call, overridable per call. The session-wide `ClientTimeout`
  could not be tightened for a single request, so the grant read — which now
  sits in front of every send — had no way to be bounded more tightly than a
  credential sign. `0` disables the deadline.

### Changed

- **Every send now performs a credential read against the node before the
  message goes out** (once per `grant_cache_ms` per DID; failures are cached
  for a shorter window so a misconfigured API key is not a per-message round
  trip). A node that cannot serve `/api/v1/credentials`, or a `DialContext`
  that only routes the WebSocket port, degrades to sending unattached — the
  previous behaviour — and `on_grant_miss` reports it. Set
  `attach_grants=False` to opt out entirely.

- Outbound writes are serialized on a per-client lock so two sends issued back
  to back cannot arrive reversed when the first one's grant read is the slower.
  The lock covers the read and the marshal only, never the channel write, so a
  slow server ack does not block the sends behind it.

- `RestClient.__init__` takes an optional `timeout_ms`. Additive; existing
  positional calls are unaffected.

[0.2.12]: https://github.com/layr8/python-sdk/releases/tag/v0.2.12
