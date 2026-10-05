"""The **Request user** seam (ADR-014 §1–§3): one identifier source per transport.

``stdio`` reads ONLY ``TREE_USER_IDENTIFIER``; every other transport (``None``
included) reads ONLY the ``horizon-actor-email`` header — the cross cases prove
neither source is a fallback for the other. The lookup runs against the real
``users`` collection of the unit-test database (case-insensitivity and regex
escaping are Mongo behaviour, not ours to fake); the HTTP request is the
boundary that is faked.
"""

import uuid
from collections.abc import AsyncIterator, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId
from starlette.requests import Request

from tree.entities.users import User
from tree.mcp.request_user import (
    ACTOR_EMAIL_HEADER,
    MISSING_ENV_MESSAGE,
    MISSING_HEADER_MESSAGE,
    USER_ENV_VAR,
    RequestUserError,
    resolve_request_user,
    unknown_identifier_message,
)


@pytest.fixture(autouse=True)
def _patched_request_user() -> None:
    """Override the package conftest's patch: this module tests the REAL seam."""

    return None


@pytest.fixture(autouse=True)
def _no_env_identifier(monkeypatch) -> None:
    """The Makefile exports ``.env``; start every test with the var unset."""

    monkeypatch.delenv(USER_ENV_VAR, raising=False)


@pytest.fixture
async def user_row() -> AsyncIterator[Callable[[str], PydanticObjectId]]:
    """Insert raw ``users`` rows (no ``after_insert`` hook); delete them after."""

    collection = User.get_pymongo_collection()
    inserted: list[PydanticObjectId] = []

    async def _insert(identifier: str) -> PydanticObjectId:
        user_id = PydanticObjectId()
        await collection.insert_one({"_id": user_id, "identifier": identifier})
        inserted.append(user_id)
        return user_id

    yield _insert

    await collection.delete_many({"_id": {"$in": inserted}})


@pytest.fixture
def http_headers(mocker) -> Callable[[dict[str, str]], None]:
    """Fake the current HTTP request with the given RAW headers.

    Patched at ``get_http_request`` — the call ``get_http_headers`` makes — so
    FastMCP's own lowercasing and filtering still run.
    """

    def _set(headers: dict[str, str]) -> None:
        raw = [(k.encode(), v.encode()) for k, v in headers.items()]
        request = Request({"type": "http", "headers": raw})
        mocker.patch(
            "fastmcp.server.dependencies.get_http_request", return_value=request
        )

    return _set


def _ctx(transport: str | None) -> MagicMock:
    ctx = MagicMock()
    ctx.transport = transport
    return ctx


def _email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@example.com"


# ---------------------------------------------------------------------------
# stdio → TREE_USER_IDENTIFIER only
# ---------------------------------------------------------------------------


async def test_stdio_resolves_the_env_identifier(monkeypatch, user_row) -> None:
    email = _email()
    user_id = await user_row(email)
    monkeypatch.setenv(USER_ENV_VAR, email)

    assert await resolve_request_user(_ctx("stdio")) == user_id


async def test_stdio_ignores_the_header(user_row, http_headers) -> None:
    email = _email()
    await user_row(email)
    http_headers({ACTOR_EMAIL_HEADER: email})

    with pytest.raises(RequestUserError) as raised:
        await resolve_request_user(_ctx("stdio"))

    assert str(raised.value) == MISSING_ENV_MESSAGE


# ---------------------------------------------------------------------------
# Every other transport → horizon-actor-email only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("transport", ["streamable-http", "sse", None])
async def test_http_resolves_the_actor_email_header(
    user_row, http_headers, transport: str | None
) -> None:
    email = _email()
    user_id = await user_row(email)
    http_headers({ACTOR_EMAIL_HEADER: email})

    assert await resolve_request_user(_ctx(transport)) == user_id


async def test_http_ignores_the_env_identifier(monkeypatch, user_row) -> None:
    email = _email()
    await user_row(email)
    monkeypatch.setenv(USER_ENV_VAR, email)

    with pytest.raises(RequestUserError) as raised:
        await resolve_request_user(_ctx("streamable-http"))

    assert str(raised.value) == MISSING_HEADER_MESSAGE


async def test_no_transport_without_a_request_takes_the_header_path(
    monkeypatch, user_row
) -> None:
    """``None`` is what FastMCP answers outside a server context: never stdio."""

    email = _email()
    await user_row(email)
    monkeypatch.setenv(USER_ENV_VAR, email)

    with pytest.raises(RequestUserError) as raised:
        await resolve_request_user(_ctx(None))

    assert str(raised.value) == MISSING_HEADER_MESSAGE


async def test_the_header_name_is_case_insensitive(user_row, http_headers) -> None:
    email = _email()
    user_id = await user_row(email)
    http_headers({"Horizon-Actor-Email": email})

    assert await resolve_request_user(_ctx("streamable-http")) == user_id


@pytest.mark.parametrize(
    "transport,message",
    [("stdio", MISSING_ENV_MESSAGE), ("streamable-http", MISSING_HEADER_MESSAGE)],
    ids=["stdio-env", "http-header"],
)
async def test_a_whitespace_only_identifier_is_missing(
    monkeypatch, http_headers, transport: str, message: str
) -> None:
    monkeypatch.setenv(USER_ENV_VAR, "   ")
    http_headers({ACTOR_EMAIL_HEADER: "   "})

    with pytest.raises(RequestUserError) as raised:
        await resolve_request_user(_ctx(transport))

    assert str(raised.value) == message


# ---------------------------------------------------------------------------
# The lookup: case-insensitive, anchored, escaped
# ---------------------------------------------------------------------------


async def test_a_mixed_case_header_finds_a_lowercase_row(
    user_row, http_headers
) -> None:
    local = f"paul-{uuid.uuid4().hex[:8]}"
    user_id = await user_row(f"{local}@example.com")
    http_headers({ACTOR_EMAIL_HEADER: f"{local.upper()}@Example.COM"})

    assert await resolve_request_user(_ctx("streamable-http")) == user_id


async def test_a_lowercase_identifier_finds_a_legacy_uppercase_row(
    monkeypatch, user_row
) -> None:
    """No migration: a row stored before signup lowercased still matches."""

    local = f"Paul-{uuid.uuid4().hex[:8]}"
    user_id = await user_row(f"{local}@Example.com")
    monkeypatch.setenv(USER_ENV_VAR, f"{local.lower()}@example.com")

    assert await resolve_request_user(_ctx("stdio")) == user_id


async def test_regex_metacharacters_are_escaped(user_row, http_headers) -> None:
    suffix = uuid.uuid4().hex[:8]
    await user_row(f"aab-{suffix}@x.com")
    http_headers({ACTOR_EMAIL_HEADER: f"a+b-{suffix}@x.com"})

    with pytest.raises(RequestUserError) as raised:
        await resolve_request_user(_ctx("streamable-http"))

    assert str(raised.value) == unknown_identifier_message(f"a+b-{suffix}@x.com")


async def test_the_match_is_anchored(user_row, http_headers) -> None:
    """A row that merely CONTAINS the identifier is not that user."""

    email = _email()
    await user_row(f"x{email}x")
    http_headers({ACTOR_EMAIL_HEADER: email})

    with pytest.raises(RequestUserError):
        await resolve_request_user(_ctx("streamable-http"))


async def test_find_one_is_awaited_with_the_anchored_case_insensitive_regex(
    mocker, monkeypatch
) -> None:
    user = MagicMock(id=PydanticObjectId())
    find_one = mocker.patch(
        "tree.entities.users.User.find_one", new_callable=AsyncMock, return_value=user
    )
    monkeypatch.setenv(USER_ENV_VAR, " A.B+c@Example.com ")

    await resolve_request_user(_ctx("stdio"))

    find_one.assert_awaited_once_with(
        {"identifier": {"$regex": r"^a\.b\+c@example\.com$", "$options": "i"}}
    )


async def test_an_unknown_identifier_names_signup_and_the_horizon_rule(
    http_headers,
) -> None:
    email = _email()
    http_headers({ACTOR_EMAIL_HEADER: email})

    with pytest.raises(RequestUserError) as raised:
        await resolve_request_user(_ctx("streamable-http"))

    message = str(raised.value)
    assert f"'{email}'" in message
    assert f"make memory-signup USER_IDENTIFIER={email}" in message
    assert "must equal your Horizon account email" in message


# ---------------------------------------------------------------------------
# The three messages — test anchors, asserted verbatim
# ---------------------------------------------------------------------------


def test_missing_header_message_is_verbatim() -> None:
    assert MISSING_HEADER_MESSAGE == (
        "No horizon-actor-email header on this request. On Prefect Horizon the "
        "gateway adds it when Horizon authentication is enabled for this server "
        '(Access mode must not be "Disabled") and the caller is a user with an '
        "email (a service account may have none). Against a local HTTP server "
        "there is no gateway: set the header on your client, e.g. "
        "fastmcp.Client(StreamableHttpTransport(url, headers="
        '{"horizon-actor-email": "<your signed-up identifier>"})).'
    )


def test_missing_env_message_is_verbatim() -> None:
    assert MISSING_ENV_MESSAGE == (
        "TREE_USER_IDENTIFIER is empty in this stdio server's environment. Set "
        'it in the "env" block of the tree-memory-local entry (.mcp.json), or '
        "in .env, which that entry loads with --env-file."
    )


def test_unknown_identifier_message_is_verbatim() -> None:
    assert unknown_identifier_message("carol@example.com") == (
        "No user with identifier 'carol@example.com' — run "
        "`make memory-signup USER_IDENTIFIER=carol@example.com` "
        "(scripts/signup.py) first; on Horizon the signed-up identifier must "
        "equal your Horizon account email."
    )
