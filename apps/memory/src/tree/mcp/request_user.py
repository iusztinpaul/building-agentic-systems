"""The **Request user** seam: who is calling, resolved per request (ADR-014 §1–§3).

Every MCP tool calls :func:`resolve_request_user` on every call; nothing else
reads the identifier. Exactly ONE source per transport, no cross-fallback:

* ``stdio`` → the process env ``TREE_USER_IDENTIFIER`` ONLY (the client's
  ``.mcp.json`` ``env`` block, or ``.env`` loaded with ``--env-file``);
* any other transport (``streamable-http``, ``sse`` — and ``None``, which
  FastMCP answers outside a server context) → the ``horizon-actor-email``
  request header ONLY. On Prefect Horizon the gateway injects it after Horizon
  authentication succeeds and strips any client-supplied ``horizon-*`` header;
  against a local HTTP server there is no gateway, so the client sets it.

Only how the identifier is injected differs per transport: everything after the
identifier is read — the case-insensitive lookup, the errors — is shared, so a
future identity source changes this function's body and nothing downstream.

Imports ``fastmcp``, ``beanie`` and :mod:`tree.entities.users` only — never
:mod:`tree.mcp.tools` — so the tool modules can import it without a cycle.
"""

import os

from beanie import PydanticObjectId
from fastmcp import Context
from fastmcp.server.dependencies import get_http_headers

from tree.entities.users import find_user_by_identifier, normalize_identifier

ACTOR_EMAIL_HEADER = "horizon-actor-email"
USER_ENV_VAR = "TREE_USER_IDENTIFIER"

#: Test anchors (the ``ERROR_CONTRACT`` pattern): asserted verbatim, so a
#: reworded message is a deliberate decision, not drift.
MISSING_HEADER_MESSAGE = (
    "No horizon-actor-email header on this request. On Prefect Horizon the "
    "gateway adds it when Horizon authentication is enabled for this server "
    '(Access mode must not be "Disabled") and the caller is a user with an '
    "email (a service account may have none). Against a local HTTP server "
    "there is no gateway: set the header on your client, e.g. "
    "fastmcp.Client(StreamableHttpTransport(url, headers="
    '{"horizon-actor-email": "<your signed-up identifier>"})).'
)

MISSING_ENV_MESSAGE = (
    "TREE_USER_IDENTIFIER is empty in this stdio server's environment. Set it "
    'in the "env" block of the tree-memory-local entry (.mcp.json), or in '
    ".env, which that entry loads with --env-file."
)


def unknown_identifier_message(identifier: str) -> str:
    """The answer for an identifier no ``users`` row matches."""

    return (
        f"No user with identifier '{identifier}' — run "
        f"`make memory-signup USER_IDENTIFIER={identifier}` (scripts/signup.py) "
        "first; on Horizon the signed-up identifier must equal your Horizon "
        "account email."
    )


class RequestUserError(Exception):
    """The request carries no usable user. The message IS the user-facing sentence.

    Tools answer it as the **Tool error envelope** ``configuration_error``,
    ``retryable: false`` — the same call fails identically until an operator
    (or the client config) changes something.
    """


async def resolve_request_user(ctx: Context) -> PydanticObjectId:
    """Resolve the ``User._id`` this request runs as.

    Raises :class:`RequestUserError` with :data:`MISSING_ENV_MESSAGE` (stdio,
    empty env), :data:`MISSING_HEADER_MESSAGE` (any other transport, no header)
    or :func:`unknown_identifier_message` (no matching ``users`` row). One
    ``users`` read per call, no cache.
    """

    if ctx.transport == "stdio":
        identifier = os.environ.get(USER_ENV_VAR, "").strip()
        missing_message = MISSING_ENV_MESSAGE
    else:
        # ``get_http_headers`` lowercases names and answers ``{}`` without an
        # HTTP request — it never raises.
        identifier = get_http_headers().get(ACTOR_EMAIL_HEADER, "").strip()
        missing_message = MISSING_HEADER_MESSAGE

    if not identifier:
        raise RequestUserError(missing_message)

    identifier = normalize_identifier(identifier)
    user = await find_user_by_identifier(identifier)
    if user is None:
        raise RequestUserError(unknown_identifier_message(identifier))
    return user.id
