# LAYR8-610: Reply Protocol, Wildcard Binding, Capability Negotiation

**Jira:** LAYR8-610
**Date:** 2026-05-15

## Overview

The cloud node (LAYR8-580) added a new plugin dispatch protocol. After each inbound message, the SDK sends a `dispatch_reply` event telling the node whether the message was handled, passed, or errored. This replaces the old ack mechanism and enables the node to route unhandled messages to other plugins.

## 1. PASS Sentinel

`src/layr8/sentinel.py` — singleton sentinel class:

- `_Pass` class: singleton via `__new__`, `__repr__` returns `"PASS"`, `__bool__` returns `False`
- Module-level `PASS = _Pass()`
- Exported from `layr8.__init__` as `PASS`

Handler return type becomes `Message | None | _Pass`.

## 2. Handler Registry Changes

`src/layr8/handler.py`:

- Remove `manual_ack` from `HandlerEntry`
- Remove `manual_ack` param from `register()`
- Add `_catch_all: HandlerEntry | None` field to `HandlerRegistry`
- Add `register_catch_all(fn)` — raises `ValueError` if already set
- `lookup(msg_type)` returns: specific match > catch-all > `None`
- `protocols()` appends `"*"` if catch-all is registered

## 3. Capability Negotiation

`src/layr8/channel.py`:

- `_join()` adds `reply_protocol: True` to join payload
- Parse `capabilities` from join reply response
- `self._reply_protocol = "reply_protocol/1" in capabilities`
- Default `False` (old servers omit capabilities)
- Expose as `PhoenixChannel.reply_protocol` property

## 4. Client Dispatch

`src/layr8/client.py`:

### New mode (reply_protocol=True)

After parsing inbound message:

1. Look up handler: specific > catch-all > none
2. No handler found → `dispatch_reply(status="pass")`, fire `NO_HANDLER` error callback
3. Handler found → run it, then based on result:
   - Returns `Message` → send message to recipient, `dispatch_reply(status="handled")`
   - Returns `None` → `dispatch_reply(status="handled")`
   - Returns `PASS` → `dispatch_reply(status="pass")`
   - Raises exception → send problem report, `dispatch_reply(status="error", code=type(e).__name__, message=str(e))`

`dispatch_reply` wire format:
```json
{
  "message_id": "<msg.id>",
  "status": "handled|pass|error",
  "code": "<only for error>",
  "message": "<only for error>"
}
```

Sent via `channel.send_fire_and_forget("dispatch_reply", payload)`.

### Legacy mode (reply_protocol=False)

Same as current behavior: auto-ack before handler, run handler, send response.

## 5. Public API Changes

### Added
- `PASS` sentinel (exported from `layr8`)
- `client.handle_all(fn)` — registers catch-all, works as decorator or direct call

### Removed
- `manual_ack` parameter on `client.handle()`
- `msg.ack()` method on `Message`
- `send_ack` from `PhoenixChannel` public API (kept private for legacy mode)

### Unchanged
- Existing handlers returning `Message` or `None` work without modification
- `client.handle(msg_type, fn)` API unchanged (minus `manual_ack`)

## 6. Testing

### Handler registry
- `register_catch_all` + lookup priority (specific > catch-all > None)
- Duplicate catch-all raises
- `protocols()` includes `"*"` with catch-all

### Client dispatch — new mode
- Handler returns `Message` → dispatch_reply "handled" + message sent
- Handler returns `None` → dispatch_reply "handled"
- Handler returns `PASS` → dispatch_reply "pass"
- Handler raises → dispatch_reply "error" + problem report
- No handler, no catch-all → dispatch_reply "pass"
- Catch-all invoked when no specific handler

### Client dispatch — legacy mode
- Old server (no capabilities) → ack behavior preserved

### Capability negotiation
- Join reply with `capabilities: ["reply_protocol/1"]` → new mode
- Join reply without capabilities → legacy mode

### Cleanup
- Remove `manual_ack` tests
- Update ack assertions to expect dispatch_reply
