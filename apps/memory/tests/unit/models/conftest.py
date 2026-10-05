"""Fixtures shared by the test modules in this package.

The Modal doubles live in :mod:`tests.unit.models.modal_fixtures`, which
``tests/unit/scripts/test_modal_model_script.py`` imports directly — a conftest
serves its own directory tree only, and that module sits in a sibling one. This
file re-exports the ones written as fixtures so every module HERE can request
them by name (PR #44, Nit 3).

The suite-wide rails (``_modal_dry_run``, ``_no_live_modal_sdk``,
``_noop_voyage_rate_limit``), ``modal_seam`` and ``run`` stay in
``tests/unit/conftest.py``, where the whole unit suite sees them.
"""

import httpx
import pytest
from prefect.concurrency.asyncio import ConcurrencySlotAcquisitionError
from prefect.exceptions import PrefectHTTPStatusError

from tests.unit.models.modal_fixtures import hub

__all__ = ["hub", "limiter_401"]


@pytest.fixture
def limiter_401() -> ConcurrencySlotAcquisitionError:
    """The limiter failure a stale ``PREFECT_API_KEY`` produces (task 178).

    prefect 3.6's ``aacquire_concurrency_slots`` wraps every acquire failure in
    ``ConcurrencySlotAcquisitionError`` and chains the client's
    ``PrefectHTTPStatusError`` as ``__cause__`` — built here exactly that way,
    with httpx's real 401 message so ``str(cause)`` names the status.
    """

    request = httpx.Request(
        "POST", "https://api.prefect.cloud/api/accounts/a/workspaces/w/v2/increment"
    )
    response = httpx.Response(
        401,
        request=request,
        json={"detail": "Invalid authentication credentials"},
    )
    cause = PrefectHTTPStatusError.from_httpx_error(
        httpx.HTTPStatusError(
            f"Client error '401 Unauthorized' for url '{request.url}'",
            request=request,
            response=response,
        )
    )
    error = ConcurrencySlotAcquisitionError(
        "Unable to acquire concurrency slots on ['voyage-embeddings']"
    )
    error.__cause__ = cause
    return error
