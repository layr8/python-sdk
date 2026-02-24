"""Error types for the Layr8 SDK."""


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
