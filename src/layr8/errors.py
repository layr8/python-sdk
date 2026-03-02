"""Error types for the Layr8 SDK."""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class Layr8Error(Exception):
    """Base class for all Layr8 SDK errors."""


class NotConnectedError(Layr8Error):
    """Raised when send/request is called before connect()."""

    def __init__(self) -> None:
        super().__init__("client is not connected")


class AlreadyConnectedError(Layr8Error):
    """Raised when handle() is called after connect()."""

    def __init__(self) -> None:
        super().__init__("client is already connected")


class ClientClosedError(Layr8Error):
    """Raised when connect() is called after close()."""

    def __init__(self) -> None:
        super().__init__("client is closed")


class ProblemReportError(Layr8Error):
    """
    Represents a DIDComm problem report received from a remote agent.

    See: https://identity.foundation/didcomm-messaging/spec/#problem-reports
    """

    def __init__(self, code: str, comment: str) -> None:
        super().__init__(f"problem report [{code}]: {comment}")
        self.code = code
        self.comment = comment


class Layr8ConnectionError(Layr8Error):
    """Represents a failure to connect to the cloud-node."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"connection error [{url}]: {reason}")
        self.url = url
        self.reason = reason


# ---------------------------------------------------------------------------
# Poka-yoke error handling
# ---------------------------------------------------------------------------


class ErrorKind(enum.Enum):
    """Categories of SDK-level errors."""

    PARSE_FAILURE = "ParseFailure"
    NO_HANDLER = "NoHandler"
    HANDLER_EXCEPTION = "HandlerException"
    SERVER_REJECT = "ServerReject"
    TRANSPORT_WRITE = "TransportWrite"


@dataclass
class SDKError:
    """
    Structured error report for SDK-level errors.

    This is NOT an exception — it's a diagnostic report delivered
    to the ErrorHandler callback.
    """

    kind: ErrorKind
    message_id: str = ""
    type: str = ""
    from_did: str = ""
    cause: Exception | None = None
    raw: Any = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        cause_msg = str(self.cause) if self.cause else "unknown"
        return f"SDKError[{self.kind.value}]: {cause_msg} (msg={self.message_id} type={self.type} from={self.from_did})"


# Type alias for error handler callback
ErrorHandler = Callable[[SDKError], None]


def log_errors(logger: logging.Logger | None = None) -> ErrorHandler:
    """
    Return an ErrorHandler that logs all SDK errors.

    If no logger is provided, uses ``logging.getLogger("layr8")``.
    """
    _logger = logger or logging.getLogger("layr8")

    def handler(err: SDKError) -> None:
        _logger.error(
            "layr8 SDK error [%s]: %s",
            err.kind.value,
            err.cause or "unknown",
            extra={
                "kind": err.kind.value,
                "message_id": err.message_id,
                "type": err.type,
                "from": err.from_did,
            },
        )

    return handler
