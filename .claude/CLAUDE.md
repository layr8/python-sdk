# Python SDK Principles

## Public Repository

This is a public repository. Every commit is visible to the world.

### Before every commit, verify:

- **No real API keys, passwords, or tokens.** Test keys must be obviously fake (e.g., contain `testkey` in the string).
- **No internal infrastructure references.** No cloud account IDs, cluster names, or internal domain names. Only `*.localhost` and `example.com` are acceptable.
- **No internal documentation links.** No references to private repos, internal wikis, or private channels.
- **No customer data or PII.**

### Acceptable

- Local-dev test keys with obvious patterns (e.g., `alice_abcd1234_testkeyalicetestkeyali24`)
- `*.localhost` URLs for local development
- `did:web:*.localhost:*` test DIDs
- Unit test sentinel values like `"test-api-key"`

### Not acceptable

- Keys that follow production format without obvious test markers
- Internal service URLs (`.internal`, `.corp`, `.svc.cluster.local`)
- `.env` files with real values (`.env.example` with placeholders is fine)

## Testing

- Run tests before every commit
- Integration tests require a local dev environment with two cloud-nodes (alice-test, bob-test)

## Cloud-Node Protocol Requirement

The cloud-node **requires at least one protocol** in `payload_types` when joining a Phoenix channel. Clients that join with an empty protocol list get rejected with `e.join.plugin.protocol.missing`.

- Handlers automatically derive protocols (e.g., registering a handler for `https://layr8.test/echo/1.0/request` advertises `https://layr8.test/echo/1.0`).
- Sender-only clients (no handlers, only `request()`) must specify protocols via `Config.protocols`.
- Compat scenario senders and receivers that don't register handlers must always pass their protocol through `Config.protocols`.

## Conventions

- Async/await with `asyncio`
- `@dataclass` for structured types (`Credential`, `StoredCredential`, `VerifiedPresentation`)
- Custom `aiohttp` resolver (`_LocalhostResolver`) for `*.localhost` resolution (RFC 6761)
- Snake_case for all public API methods (e.g., `sign_credential`, `verify_presentation`)
