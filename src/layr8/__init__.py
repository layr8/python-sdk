"""Layr8 DIDComm Agent SDK for Python."""

from .client import Client
from .config import Config, GrantMissInfo
from .credentials import Credential, StoredCredential, VerifiedCredential
from .errors import (
    AlreadyConnectedError,
    ClientClosedError,
    ErrorKind,
    Layr8ConnectionError,
    Layr8Error,
    NotConnectedError,
    ProblemReportError,
    SDKError,
    ServerRejectError,
    log_errors,
)
# Identity credentials — "who the sender is", as opposed to the grants the
# wallet attaches, which say what it may do. Selection is the CALLER's,
# always: see the module doc for why an SDK must not choose these for you.
from .identity import (
    CREDENTIAL_MEDIA_TYPE,
    identity_attachment,
    is_identity_attachment,
)
from .mcp import DEFAULT_MCP_BASE, McpBinding, McpError, McpPeer
from .message import Attachment, AttachmentData, Credential, Message, MessageContext
from .presentations import VerifiedPresentation
from .rest import RESTError
from .sentinel import PASS
from .space_watch import SpaceWatcher, order_independent_signature
from .wallet import HeldCredential, Wallet

__all__ = [
    "Attachment",
    "AttachmentData",
    "identity_attachment",
    "is_identity_attachment",
    "CREDENTIAL_MEDIA_TYPE",
    "Client",
    "Config",
    "GrantMissInfo",
    "Wallet",
    "HeldCredential",
    "SpaceWatcher",
    "order_independent_signature",
    "McpBinding",
    "McpPeer",
    "McpError",
    "DEFAULT_MCP_BASE",
    "Message",
    "MessageContext",
    "Credential",
    "VerifiedCredential",
    "StoredCredential",
    "VerifiedPresentation",
    "RESTError",
    "Layr8Error",
    "NotConnectedError",
    "AlreadyConnectedError",
    "ClientClosedError",
    "ProblemReportError",
    "ServerRejectError",
    "Layr8ConnectionError",
    "ErrorKind",
    "SDKError",
    "log_errors",
    "PASS",
]
