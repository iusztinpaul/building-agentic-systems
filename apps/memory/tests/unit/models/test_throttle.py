"""Unit tests for :func:`tree.models.throttle.acquire_voyage_slot` (task 178).

The ``voyage-embeddings`` acquire is a courtesy to the shared free-tier key,
not the quota guard (Voyage's own 429 backoff is). So a limiter that cannot be
REACHED — auth, connection, HTTP status, timeout — logs one WARNING and lets
the caller embed; anything else is a bug and still propagates (ADR-013 §5).
"""

import asyncio
import logging
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from prefect.concurrency.asyncio import (
    AcquireConcurrencySlotTimeoutError,
    ConcurrencySlotAcquisitionError,
)

from tree.config.app_config import app_config
from tree.models.throttle import VOYAGE_EMBED_LIMIT, acquire_voyage_slot

_FAIL_OPEN_ANCHOR = "embedding without throttle"


def _fail_open_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and _FAIL_OPEN_ANCHOR in record.getMessage()
    ]


def _wrapped(cause: BaseException) -> ConcurrencySlotAcquisitionError:
    """Wrap ``cause`` the way prefect's ``aacquire_concurrency_slots`` does."""

    error = ConcurrencySlotAcquisitionError(
        "Unable to acquire concurrency slots on ['voyage-embeddings']"
    )
    error.__cause__ = cause
    return error


def _timeout() -> AcquireConcurrencySlotTimeoutError:
    """A timeout as prefect raises it: chained to a message-less ``TimeoutError``."""

    error = AcquireConcurrencySlotTimeoutError(
        "Attempt to acquire concurrency slots timed out after 5.0 second(s)"
    )
    error.__cause__ = TimeoutError()
    return error


class TestAcquireVoyageSlot:
    async def test_reachable_limiter_acquires_one_slot_and_warns_nothing(
        self, mocker, caplog
    ) -> None:
        # Arrange
        rate_limit = mocker.patch(
            "tree.models.throttle.rate_limit", new_callable=AsyncMock
        )

        # Act
        with caplog.at_level(logging.WARNING, logger="tree.models.throttle"):
            await acquire_voyage_slot()

        # Assert
        rate_limit.assert_awaited_once_with(
            VOYAGE_EMBED_LIMIT,
            occupy=1,
            timeout_seconds=app_config.concurrency.voyage_slot_acquire_timeout_seconds,
            strict=False,
        )
        assert _fail_open_warnings(caplog) == []

    def test_acquire_bound_defaults_well_above_one_slot_decay(self) -> None:
        # 120 s = runner_global_limit (6) x one slot's decay (60 / voyage_rpm = 20 s).
        assert app_config.concurrency.voyage_slot_acquire_timeout_seconds == 120.0

    async def test_hanging_limiter_fails_open_within_the_acquire_bound(
        self, mocker, monkeypatch, caplog
    ) -> None:
        # Arrange: the limiter accepts the request but never replies.
        async def hang(*args: object, **kwargs: object) -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(
            app_config.concurrency, "voyage_slot_acquire_timeout_seconds", 0.05
        )
        mocker.patch("tree.models.throttle.rate_limit", side_effect=hang)

        # Act
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="tree.models.throttle"):
            await acquire_voyage_slot()
        elapsed = time.monotonic() - started

        # Assert: returned at the bound (not minutes later) with ONE WARNING.
        assert elapsed < 1.0
        warnings = _fail_open_warnings(caplog)
        assert len(warnings) == 1
        assert "no reply within 0.05 s" in warnings[0]

    async def test_throttling_wait_inside_the_bound_is_not_cut_short(
        self, mocker, monkeypatch, caplog
    ) -> None:
        # Arrange: a genuinely busy slot — the acquire blocks, then succeeds.
        acquired: list[bool] = []

        async def busy_slot(*args: object, **kwargs: object) -> None:
            await asyncio.sleep(0.1)
            acquired.append(True)

        monkeypatch.setattr(
            app_config.concurrency, "voyage_slot_acquire_timeout_seconds", 2.0
        )
        mocker.patch("tree.models.throttle.rate_limit", side_effect=busy_slot)

        # Act
        with caplog.at_level(logging.WARNING, logger="tree.models.throttle"):
            await acquire_voyage_slot()

        # Assert
        assert acquired == [True]
        assert _fail_open_warnings(caplog) == []

    def test_limit_name_is_voyage_embeddings(self) -> None:
        assert VOYAGE_EMBED_LIMIT == "voyage-embeddings"

    async def test_stale_api_key_warns_once_naming_the_401(
        self, mocker, caplog, limiter_401
    ) -> None:
        # Arrange: the live Horizon failure — a 401 chained under prefect's wrapper.
        mocker.patch(
            "tree.models.throttle.rate_limit",
            new_callable=AsyncMock,
            side_effect=limiter_401,
        )

        # Act
        with caplog.at_level(logging.WARNING, logger="tree.models.throttle"):
            await acquire_voyage_slot()

        # Assert: one WARNING, carrying the CAUSE (the 401), not the wrapper.
        warnings = _fail_open_warnings(caplog)
        assert len(warnings) == 1
        assert warnings[0].startswith(
            "Prefect rate limiter 'voyage-embeddings' unavailable: "
        )
        assert "401 Unauthorized" in warnings[0]

    @pytest.mark.parametrize(
        ("error", "expected_detail"),
        [
            pytest.param(_timeout(), "timed out after 5.0", id="acquire-timeout"),
            pytest.param(
                httpx.ConnectError("[Errno 61] Connection refused"),
                "Connection refused",
                id="bare-connect-error",
            ),
            pytest.param(
                _wrapped(httpx.ConnectError("[Errno 61] Connection refused")),
                "Connection refused",
                id="wrapped-connect-error",
            ),
            pytest.param(
                httpx.ReadTimeout("timed out reading"),
                "timed out reading",
                id="bare-read-timeout",
            ),
        ],
    )
    async def test_unreachable_limiter_warns_once_and_returns(
        self, mocker, caplog, error: BaseException, expected_detail: str
    ) -> None:
        # Arrange
        mocker.patch(
            "tree.models.throttle.rate_limit",
            new_callable=AsyncMock,
            side_effect=error,
        )

        # Act
        with caplog.at_level(logging.WARNING, logger="tree.models.throttle"):
            await acquire_voyage_slot()

        # Assert
        warnings = _fail_open_warnings(caplog)
        assert len(warnings) == 1
        assert expected_detail in warnings[0]

    async def test_bare_prefect_http_status_error_warns_once(
        self, mocker, caplog, limiter_401
    ) -> None:
        # Arrange: the unwrapped client error (PrefectHTTPStatusError itself).
        mocker.patch(
            "tree.models.throttle.rate_limit",
            new_callable=AsyncMock,
            side_effect=limiter_401.__cause__,
        )

        # Act
        with caplog.at_level(logging.WARNING, logger="tree.models.throttle"):
            await acquire_voyage_slot()

        # Assert
        warnings = _fail_open_warnings(caplog)
        assert len(warnings) == 1
        assert "401 Unauthorized" in warnings[0]

    async def test_any_other_limiter_exception_propagates(self, mocker, caplog) -> None:
        # Arrange: not "cannot reach" — a bug, which must not be swallowed.
        mocker.patch(
            "tree.models.throttle.rate_limit",
            new_callable=AsyncMock,
            side_effect=RuntimeError("limiter bug"),
        )

        # Act / Assert
        with caplog.at_level(logging.WARNING, logger="tree.models.throttle"):
            with pytest.raises(RuntimeError, match="limiter bug"):
                await acquire_voyage_slot()
        assert _fail_open_warnings(caplog) == []
