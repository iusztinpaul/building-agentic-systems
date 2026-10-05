"""The **Request user** seam, patched for every MCP tool test.

Every tool resolves its user through ``tree.mcp.request_user
.resolve_request_user`` (ADR-014 §1). A ``MagicMock`` ctx's ``transport`` is
not ``"stdio"``, so an UNPATCHED tool test would take the header path, find no
``horizon-actor-email`` and answer ``configuration_error`` — so the seam is
patched here, once, for the whole package.

* A module whose fake rows are keyed on a specific id overrides
  ``request_user_id`` with its own constant (plain pytest fixture overriding).
* A test that needs the seam to raise patches the same target itself; its
  patch is applied inside the test and wins.
* ``test_request_user.py`` tests the REAL seam, so it overrides
  ``_patched_request_user`` with a no-op.
"""

from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId


@pytest.fixture
def request_user_id() -> PydanticObjectId:
    """The id the patched seam resolves to."""

    return PydanticObjectId()


@pytest.fixture(autouse=True)
def _patched_request_user(mocker, request_user_id) -> AsyncMock:
    return mocker.patch(
        "tree.mcp.request_user.resolve_request_user",
        new_callable=AsyncMock,
        return_value=request_user_id,
    )
