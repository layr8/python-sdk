# Changelog

All notable changes to `layr8`. Format loosely follows [Keep a Changelog](https://keepachangelog.com/); versioning follows [SemVer](https://semver.org/).

This file starts here. Earlier releases are recorded only in git history.

## [Unreleased]

### Added

- **A borrowed child's delegated set is kept current while it is connected.**
  A join that names a `parent_did` now sends `delegation_refresh: true`. When
  the node announces `ephemeral_delegation_refresh/1`, it pushes a
  `delegated_credentials` event with the whole new set whenever the parent's
  grants change. The client replaces the reading and the attached credentials,
  ignores a push whose `revision` is not newer than the one it holds, ignores a
  push that does not parse (and an `unread` push, which the node never sends),
  and calls `on_delegation(did, reading)`. A rejoin starts the revision again
  from the join reply.
- `Client.supports_ephemeral_delegation_refresh()`, `Client.on_delegation()`,
  `layr8.DELEGATION_REFRESH_CAPABILITY` and
  `layr8.delegated.parse_delegation_push()`.

## [0.3.1] - 2026-09-16

### Added

- **The `trace_context` plaintext header is carried.** A DIDComm message may
  carry a W3C trace context in a top-level `trace_context` object
  (`traceparent`, optional `tracestate`). The SDK used to drop it on parse and
  never wrote it. It is now `Message.trace_context`, a dict with the keys
  `traceparent` and optionally `tracestate`: `parse_didcomm` reads it,
  `marshal_didcomm` writes it, and `send()` / `request()` carry a value the
  caller sets. `read_trace_context` (exported) is the one reader.
- **A handler's reply joins the request's trace.** The auto-filled reply
  copies the request's `trace_context` unchanged unless the handler set its
  own, next to where it already defaults `thread_id`. The problem report sent
  for a failed handler copies it too.

  A value that is not a dict with a string `traceparent` is dropped, never a
  parse error, and members other than `traceparent` and `tracestate` are not
  forwarded. The SDK does not validate the `traceparent` format. It does not
  yet create a trace context for a new request that has none. The node, Go and
  Elixir SDKs make the same change.

## [0.3.0] - 2026-09-15

### Changed

Both entries below change an existing API, so this release is a minor, not a
patch (RELEASING.md, "Choosing the version"). The Go SDK shipped the same
`lastmod_time` change as a breaking one in v0.2.0.

- **`Attachment.lastmod_time` is `int | str | None`, not `int | None`.** DIDComm
  v2 states no type for the field: its Attachments section says only "OPTIONAL. A
  hint about when the content in this attachment was last modified", while the
  same document pins `created_time` and `expires_time` to "UTC Epoch Seconds
  (seconds since 1970-01-01T00:00:00Z) as an integer". The omission is visible
  rather than accidental, so a receiver is not entitled to demand an integer.
  Senders have put both an integer and an RFC 3339 string on the wire.

  Nothing converted the value before and nothing converts it now — this SDK
  passes the hint through in both directions. What changes is the annotation,
  which used to approve `att.lastmod_time + 60` on a value that was in fact a
  `str`. A reader that wants a number now has to narrow, and `tests/
  test_message_lastmod_time.py` asserts the three forms — absent, integer,
  string — stay three distinct values. It also asserts the annotation itself, so
  narrowing the field back fails here rather than in a caller: Python does not
  enforce an annotation at runtime, and every other test in this file passes
  with the field declared `int`.

- **`Message.attachments` is `None` when the `attachments` header could not be
  read**, with the reason in the new `Message.attachments_unread`. An absent
  header still reads as `[]`. Returning `[]` for a header nobody decoded would
  report "this message carried no attachments", which is a measurement that was
  never taken.

### Fixed

- **`Client.sign_credential` fills an empty `id` and `issuer` before sending.**
  The node refuses a credential that lacks either field with HTTP 422 "Invalid
  credential: missing required fields", and does not say which one is missing,
  while `Credential` declares both optional with an empty default. A credential
  built with only `credential_subject` could therefore never be signed. The
  request body now carries `issuer` set to the DID the credential is signed with
  (the `issuer_did` argument, else the agent DID) and `id` set to a new
  `urn:uuid:<uuid4>`. A value the caller set is sent unchanged, and the caller's
  `Credential` instance is not modified. The Node, Go and Elixir SDKs make the
  same change.

- **An undecodable attachment no longer costs the caller the message.** An
  `attachments` header that was not a list, an entry that was not an object, or
  an attachment whose `data` was not an object raised out of `parse_didcomm`;
  the client reported a parse failure and dropped the message before routing.
  A caller waiting on `request()` then heard nothing until its own timeout, so
  a refusal arrived as silence. Attachments are now decoded in a second pass:
  the message is delivered, and the header it could not read is reported as
  unread. `Mediation`'s live-delivery handler no longer raises on such a
  message either.

## [0.2.16] - 2026-09-10

### Added

- **A join can name the parent whose authority its DID borrows, and this SDK
  derives the name.** `Config.parent_did` is optional and is sent only when set,
  so a join that names no parent puts exactly the payload on the wire it did
  before — asserted byte for byte in `tests/test_borrowed_did.py`. Pass
  `parent_did` and leave `agent_did` empty, and the client joins as
  `<parent_did>:<segment>`: twelve characters of Crockford base32 from
  `secrets`, generated once when the configuration is resolved, so a reconnect
  returns under the same DID and the node re-mints the same credentials for it.

  **The reason the shape is fixed:** a cloud-node API key restricts which DIDs
  it may bind, and an entry is either an exact DID or a prefix with a trailing
  `*`. While a borrower's name was unrelated to its parent — and generated per
  connection — no entry could be written for it in advance, so the only key that
  admitted a borrower was one with *no restrictions at all*, which admits every
  DID on the node. Named beneath its parent, one key carrying the parent and
  `did_namespace_of(parent)` admits the parent and its borrowers and nothing
  else.

  A caller that supplies its own `agent_did` that is **not** named beneath the
  parent gets a `Layr8Error` from `Client(...)`, before anything is written: the
  node refuses that join with `e.join.plugin.child.not-beneath-parent`, and a
  refusal at connect time in production is the expensive way to learn this.

  New exports: `parent_did`, `did_namespace_of`, `is_beneath_parent`,
  `random_child_segment`, `resolve_borrower_did`, `CHILD_SEGMENT_LENGTH`,
  `ChildNameSource`.

  `did_spec.childNameSource` is sent alongside `parentDid` — `"sdk"` when this
  library generated the segment, `"client"` when the caller supplied the whole
  DID, and the key is **absent** when neither applies. A generated name and a
  hand-built one that conforms are otherwise identical bytes, so without it a
  malformed borrower DID could not be told apart as this library's defect from a
  caller's typo. The absent case is never folded into `"client"`.

- **The join reply carries the credentials the node signed for this DID.**
  `Client.delegated_credentials()` returns a `DelegatedCredentialsReading` —
  `status` and `credentials` — with one entry per grant the named parent holds.
  The node signs them at join, narrowed to no more than the parent carries and
  citing it in `credentialSubject.delegation.parentCapability`. When
  `attach_grants` is on they are attached to outbound messages automatically;
  there is nothing to wire up.

  **Four readings from that method, and six with
  `Client.supports_ephemeral_delegation()`. Collapsing any pair reports
  something nobody measured.**

  | `delegated_credentials()` | `supports_ephemeral_delegation()` | Meaning |
  |---|---|---|
  | `None` | `True` | the join named no parent |
  | `("complete", [])` | `True` | the parent's wallet was **read** and it grants nothing |
  | `("complete", [...])` | `True` | read, and here is all of it |
  | `("partial", [...])` | `True` | read, and some of it could not be delegated — there is more you did not get |
  | `("unread", [])` | `True` | the wallet could **not** be read; the `[]` measures nothing |
  | `None` | `False` | the node never looked |

  Anything that is not a well-formed reading — absent, a bare list from an older
  node, an unknown status — is `None`, never an empty `complete` one: that would
  state that a wallet was read and grants nothing, which is the one thing none
  of those inputs says.

  A reading arrives on **every** join and rejoin, including one that carries no
  reading at all — that clears whatever the previous join seeded, because the
  node mints a fresh set per join and the previous set names a DID document a
  rejoin may have replaced.

  **The credential exists nowhere but the join reply.** The node stores nothing
  about it, so `GET /api/v1/credentials` will never return it; rejoin to be
  issued a new one. It is not individually revocable — authority is withdrawn by
  revoking or expiring the parent's grant. Because that endpoint is not their
  source, a failed read of it no longer withholds them from a message they
  cover.

### Changed

- **A join that names a parent is sent with `storage: "ephemeral"`**, overriding
  the rule added in 0.2.15 that a fixed `agent_did` joins as a persistent twin.
  Only a temporary identity may borrow authority: the node refuses `persistent`
  + `parentDid` with `e.join.plugin.child.storage-not-ephemeral`. A borrowed DID
  is a fixed `agent_did` by construction — it is `<parent>:<segment>`, settled
  once so a reconnect returns under the same name — so without this override
  every borrowed join would have been refused. A join that names no parent is
  unaffected.

## [0.2.15] - 2026-09-09

### Fixed

- **A fixed identity joins as a persistent twin.** `phx_join` sent
  `did_spec.storage: "ephemeral"` for every join, including one with a fixed
  `agent_did`. Since cloud-node 4.19.3x (2026-09-08) an ephemeral
  twin is reclaimed the moment it disconnects, and everything stored on the
  twin — its mediator declaration above all — dies with it: messages sent to
  the agent while it was offline were dropped at the node instead of queued
  by its mediator, with no problem-report. The channel now sends
  `"persistent"` when `agent_did` is set and `"ephemeral"` only for a
  node-assigned per-session DID. No API change.

## [0.2.14] - 2026-09-04

### Added

- **Mediation — store-and-forward through a Space mediator.** An agent that
  is not always connected gives the client a mediator DID (`mediator` /
  `LAYR8_MEDIATOR_DID`) and, on every connect and reconnect, the client
  enrols (`mediate-request`, `recipient-update`), declares the mediator on
  its own node (`PUT /api/v1/dids/:did/mediator`, cloud-node ADR 0005),
  collects everything queued (`delivery-request` → re-injection through the
  node's `/didcomm` → `messages-received`) and turns live delivery on,
  handling the mediator's `delivery` pushes the same way. The SDK never
  decrypts: the mediator holds the original ciphertext and the node verifies
  it on re-injection as a first arrival, so collected messages reach your
  handlers with their original `from_`. Each step lives in `layr8.mediation`
  and returns a result with `ok` rather than raising. New config
  `mediator_live`, `didcomm_url` (`LAYR8_MEDIATOR_LIVE`, `LAYR8_DIDCOMM_URL`);
  `client.mediator`, `client.didcomm_url`; `ErrorKind.MEDIATION`;
  `post_didcomm()`; `RestClient.put/delete`.

## [0.2.13] - 2026-08-21

### Added

- `identity_attachment(credential_jws)` — a first-class way to attach an
  **identity credential** (a credential about who the sender is, with no
  `credentialSubject.scope`) so it reaches the cloud-node's
  `sender_credentials` policy input, where a grant's `senderCredentials`
  requirement can see it. It builds the attachment; **the caller names the
  credential**. The SDK does not choose: the requirement being satisfied lives
  in the recipient's grant and never reaches the sender, so automatic selection
  could only mean "attach everything the holder has", which is a disclosure
  decision, not a convenience. Raises `ValueError` on anything that is not a
  compact JWS, and on a credential that carries a scope — that is a grant, and
  attached this way it would be routed as one and satisfy nothing.
- `is_identity_attachment(attachment)`, the same test applied to an attachment
  already on a message.

### Changed

- Caller-supplied attachments still displace the wallet, with one narrowing:
  when they are **all** identity credentials, the wallet's grants are appended
  after them instead. Saying who you are must not stop you saying what you may
  do — under the old rule it did, and the node's denial then read "no grant
  covers this call". Anything else a caller attaches behaves exactly as before.

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

[0.3.1]: https://github.com/layr8/python-sdk/releases/tag/v0.3.1
[0.3.0]: https://github.com/layr8/python-sdk/releases/tag/v0.3.0
[0.2.16]: https://github.com/layr8/python-sdk/releases/tag/v0.2.16
[0.2.15]: https://github.com/layr8/python-sdk/releases/tag/v0.2.15
[0.2.14]: https://github.com/layr8/python-sdk/releases/tag/v0.2.14
[0.2.13]: https://github.com/layr8/python-sdk/releases/tag/v0.2.13
[0.2.12]: https://github.com/layr8/python-sdk/releases/tag/v0.2.12
