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
