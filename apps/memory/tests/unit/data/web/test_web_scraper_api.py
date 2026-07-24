"""Unit tests for tree.data.web.web_scraper_api.collect.

Every HTTP interaction is mocked — this suite never calls Bright Data live.
"""

import itertools
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tree.data.web.web_scraper_api import (
    BrightDataConfigurationError,
    BrightDataRequestError,
    BrightDataTimeoutError,
    collect,
)

DATASET_ID = "gd_test_dataset"
INPUTS = [{"url": "https://www.youtube.com/watch?v=abc123"}]

# Every branch of ``httpx.TransportError``: the request never completed, so
# Bright Data is unreachable right now — the condition ADR-004 Decision 3 routes
# to the Gemini fallback.
_TRANSPORT_ERROR_TYPES = [
    httpx.TimeoutException,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.PoolTimeout,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
]


def _patch_settings(mocker, *, api_key: str = "test-api-key") -> None:
    """Patch the settings singleton in the web_scraper_api module."""

    fake_secret = MagicMock()
    fake_secret.get_secret_value.return_value = api_key

    fake_settings = MagicMock()
    fake_settings.brightdata_api_key = fake_secret

    mocker.patch("tree.data.web.web_scraper_api.settings", fake_settings)


def _patch_http(mocker, *, post_return, get_side_effect) -> tuple[AsyncMock, AsyncMock]:
    """Patch the two thin HTTP seams; return the (post, get) mocks."""

    post_mock = mocker.patch(
        "tree.data.web.web_scraper_api._post_json",
        new_callable=AsyncMock,
        return_value=post_return,
    )
    get_mock = mocker.patch(
        "tree.data.web.web_scraper_api._get_json",
        new_callable=AsyncMock,
        side_effect=get_side_effect,
    )
    return post_mock, get_mock


def _patch_sleep(mocker) -> AsyncMock:
    """Replace the poll sleep so tests never wait in real time."""

    return mocker.patch(
        "tree.data.web.web_scraper_api.asyncio.sleep", new_callable=AsyncMock
    )


def _stepping_clock(step: float) -> Callable[[], float]:
    """Monotonic clock stub advancing by ``step`` seconds on every read."""

    ticks = itertools.count(0.0, step)
    return lambda: next(ticks)


def _patch_async_client(mocker, response: httpx.Response) -> AsyncMock:
    """Patch httpx.AsyncClient so the real HTTP helpers run against a stub."""

    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.get = AsyncMock(return_value=response)

    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=client)
    client_cm.__aexit__ = AsyncMock(return_value=None)

    mocker.patch(
        "tree.data.web.web_scraper_api.httpx.AsyncClient", return_value=client_cm
    )
    return client


def _patch_failing_async_client(mocker, error: Exception) -> None:
    """Patch httpx.AsyncClient so every request fails with ``error``."""

    client = AsyncMock()
    client.post = AsyncMock(side_effect=error)
    client.get = AsyncMock(side_effect=error)

    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=client)
    client_cm.__aexit__ = AsyncMock(return_value=None)

    mocker.patch(
        "tree.data.web.web_scraper_api.httpx.AsyncClient", return_value=client_cm
    )


class TestCollectConfiguration:
    async def test_raises_configuration_error_when_api_key_empty(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker, api_key="")
        post_mock, get_mock = _patch_http(
            mocker, post_return={"snapshot_id": "sd_1"}, get_side_effect=[]
        )

        # Act & Assert
        with pytest.raises(BrightDataConfigurationError, match="BRIGHTDATA_API_KEY"):
            await collect(
                DATASET_ID, INPUTS, timeout_seconds=60.0, poll_interval_seconds=1.0
            )

        post_mock.assert_not_awaited()
        get_mock.assert_not_awaited()


class TestCollectEmptyInputs:
    async def test_returns_empty_list_without_any_http_call(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        post_mock, get_mock = _patch_http(
            mocker, post_return={"snapshot_id": "sd_1"}, get_side_effect=[]
        )

        # Act
        result = await collect(
            DATASET_ID, [], timeout_seconds=60.0, poll_interval_seconds=1.0
        )

        # Assert
        assert result == []
        post_mock.assert_not_awaited()
        get_mock.assert_not_awaited()


class TestCollectHappyPath:
    async def test_returns_downloaded_records(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        records = [{"url": "https://www.youtube.com/watch?v=abc123", "title": "Demo"}]
        _patch_http(
            mocker,
            post_return={"snapshot_id": "sd_1"},
            get_side_effect=[{"status": "running"}, {"status": "ready"}, records],
        )
        _patch_sleep(mocker)

        # Act
        result = await collect(
            DATASET_ID, INPUTS, timeout_seconds=60.0, poll_interval_seconds=1.0
        )

        # Assert
        assert result == records

    async def test_stops_polling_immediately_when_first_status_is_ready(
        self, mocker
    ) -> None:
        # Arrange
        _patch_settings(mocker)
        _patch_http(
            mocker,
            post_return={"snapshot_id": "sd_1"},
            get_side_effect=[{"status": "ready"}, []],
        )
        sleep_mock = _patch_sleep(mocker)

        # Act
        await collect(
            DATASET_ID, INPUTS, timeout_seconds=60.0, poll_interval_seconds=7.0
        )

        # Assert
        sleep_mock.assert_not_awaited()

    async def test_sleeps_the_requested_poll_interval_between_polls(
        self, mocker
    ) -> None:
        # Arrange
        _patch_settings(mocker)
        _patch_http(
            mocker,
            post_return={"snapshot_id": "sd_1"},
            get_side_effect=[{"status": "starting"}, {"status": "ready"}, []],
        )
        sleep_mock = _patch_sleep(mocker)

        # Act
        await collect(
            DATASET_ID, INPUTS, timeout_seconds=600.0, poll_interval_seconds=3.5
        )

        # Assert
        sleep_mock.assert_awaited_once_with(3.5)

    async def test_calls_trigger_progress_and_snapshot_endpoints(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker, api_key="my-key")
        post_mock, get_mock = _patch_http(
            mocker,
            post_return={"snapshot_id": "sd_42"},
            get_side_effect=[{"status": "ready"}, []],
        )
        _patch_sleep(mocker)

        # Act
        await collect(
            DATASET_ID, INPUTS, timeout_seconds=60.0, poll_interval_seconds=1.0
        )

        # Assert
        trigger_call = post_mock.await_args
        assert trigger_call.args[0] == "https://api.brightdata.com/datasets/v3/trigger"
        assert trigger_call.kwargs["params"] == {
            "dataset_id": DATASET_ID,
            "format": "json",
        }
        assert trigger_call.kwargs["payload"] == {"input": INPUTS}
        assert trigger_call.kwargs["api_key"] == "my-key"

        progress_call, snapshot_call = get_mock.await_args_list
        assert (
            progress_call.args[0]
            == "https://api.brightdata.com/datasets/v3/progress/sd_42"
        )
        assert progress_call.kwargs["params"] is None
        assert (
            snapshot_call.args[0]
            == "https://api.brightdata.com/datasets/v3/snapshot/sd_42"
        )
        assert snapshot_call.kwargs["params"] == {"format": "json"}


class TestCollectFailures:
    async def test_raises_request_error_when_status_is_failed(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        _patch_http(
            mocker,
            post_return={"snapshot_id": "sd_boom"},
            get_side_effect=[{"status": "failed"}],
        )
        _patch_sleep(mocker)

        # Act & Assert
        with pytest.raises(BrightDataRequestError, match="sd_boom"):
            await collect(
                DATASET_ID, INPUTS, timeout_seconds=60.0, poll_interval_seconds=1.0
            )

    async def test_raises_request_error_when_trigger_omits_snapshot_id(
        self, mocker
    ) -> None:
        # Arrange
        _patch_settings(mocker)
        _patch_http(mocker, post_return={"error": "bad dataset"}, get_side_effect=[])

        # Act & Assert
        with pytest.raises(BrightDataRequestError, match="snapshot_id"):
            await collect(
                DATASET_ID, INPUTS, timeout_seconds=60.0, poll_interval_seconds=1.0
            )

    async def test_raises_request_error_when_snapshot_is_not_a_list(
        self, mocker
    ) -> None:
        # Arrange
        _patch_settings(mocker)
        _patch_http(
            mocker,
            post_return={"snapshot_id": "sd_1"},
            get_side_effect=[{"status": "ready"}, {"error": "gone"}],
        )
        _patch_sleep(mocker)

        # Act & Assert
        with pytest.raises(BrightDataRequestError, match="sd_1"):
            await collect(
                DATASET_ID, INPUTS, timeout_seconds=60.0, poll_interval_seconds=1.0
            )

    async def test_raises_timeout_error_naming_snapshot_id(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        _patch_http(
            mocker,
            post_return={"snapshot_id": "sd_slow"},
            get_side_effect=[{"status": "running"}] * 10,
        )
        _patch_sleep(mocker)
        mocker.patch(
            "tree.data.web.web_scraper_api.time.monotonic",
            side_effect=_stepping_clock(10.0),
        )

        # Act & Assert
        with pytest.raises(BrightDataTimeoutError, match="sd_slow"):
            await collect(
                DATASET_ID, INPUTS, timeout_seconds=30.0, poll_interval_seconds=10.0
            )


class TestHttpErrorPropagation:
    @pytest.mark.parametrize(
        "status_code",
        [400, 401, 403, 404, 429, 500, 503],
        ids=["400", "401", "403", "404", "429", "500", "503"],
    )
    async def test_trigger_non_2xx_raises_request_error_with_status_and_body(
        self, mocker, status_code: int
    ) -> None:
        # Arrange
        _patch_settings(mocker)
        request = httpx.Request(
            "POST", "https://api.brightdata.com/datasets/v3/trigger"
        )
        response = httpx.Response(
            status_code=status_code, text="upstream boom", request=request
        )
        _patch_async_client(mocker, response)

        # Act & Assert
        with pytest.raises(BrightDataRequestError, match="upstream boom") as exc_info:
            await collect(
                DATASET_ID, INPUTS, timeout_seconds=60.0, poll_interval_seconds=1.0
            )
        assert str(status_code) in str(exc_info.value)

    @pytest.mark.parametrize(
        "body",
        [
            "<html><body>Attention Required! | Cloudflare</body></html>",
            "",
            "   ",
        ],
        ids=["captcha-html", "empty", "whitespace"],
    )
    async def test_2xx_with_non_json_body_raises_request_error(
        self, mocker, body: str
    ) -> None:
        # Arrange — a WAF/captcha/empty page served with HTTP 200 must not leak
        # a raw JSONDecodeError past the fallback chain (ADR-004, Decision 3).
        _patch_settings(mocker)
        request = httpx.Request(
            "POST", "https://api.brightdata.com/datasets/v3/trigger"
        )
        response = httpx.Response(status_code=200, text=body, request=request)
        _patch_async_client(mocker, response)

        # Act & Assert
        with pytest.raises(BrightDataRequestError) as exc_info:
            await collect(
                DATASET_ID, INPUTS, timeout_seconds=60.0, poll_interval_seconds=1.0
            )

        message = str(exc_info.value)
        assert "200" in message
        assert "api.brightdata.com/datasets/v3/trigger" in message

    async def test_non_json_body_is_truncated_in_the_error_message(
        self, mocker
    ) -> None:
        # Arrange
        _patch_settings(mocker)
        request = httpx.Request(
            "POST", "https://api.brightdata.com/datasets/v3/trigger"
        )
        response = httpx.Response(
            status_code=200, text="<div>" + ("x" * 10_000) + "</div>", request=request
        )
        _patch_async_client(mocker, response)

        # Act & Assert
        with pytest.raises(BrightDataRequestError) as exc_info:
            await collect(
                DATASET_ID, INPUTS, timeout_seconds=60.0, poll_interval_seconds=1.0
            )

        assert len(str(exc_info.value)) < 1_000

    async def test_non_json_snapshot_body_raises_request_error(self, mocker) -> None:
        # Arrange — the failure can hit any of the three calls, not just trigger.
        _patch_settings(mocker)
        mocker.patch(
            "tree.data.web.web_scraper_api._post_json",
            new_callable=AsyncMock,
            return_value={"snapshot_id": "sd_1"},
        )
        mocker.patch(
            "tree.data.web.web_scraper_api._wait_until_ready", new_callable=AsyncMock
        )
        request = httpx.Request(
            "GET", "https://api.brightdata.com/datasets/v3/snapshot/sd_1"
        )
        response = httpx.Response(
            status_code=200, text="<html>rate limited</html>", request=request
        )
        _patch_async_client(mocker, response)

        # Act & Assert
        with pytest.raises(BrightDataRequestError, match="200"):
            await collect(
                DATASET_ID, INPUTS, timeout_seconds=60.0, poll_interval_seconds=1.0
            )

    async def test_trigger_sends_bearer_auth_header(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker, api_key="secret-key")
        request = httpx.Request(
            "POST", "https://api.brightdata.com/datasets/v3/trigger"
        )
        response = httpx.Response(
            status_code=200, json={"snapshot_id": "sd_1"}, request=request
        )
        client = _patch_async_client(mocker, response)
        mocker.patch(
            "tree.data.web.web_scraper_api._get_json",
            new_callable=AsyncMock,
            side_effect=[{"status": "ready"}, []],
        )

        # Act
        await collect(
            DATASET_ID, INPUTS, timeout_seconds=60.0, poll_interval_seconds=1.0
        )

        # Assert
        headers = client.post.await_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer secret-key"


class TestTransportErrorTyping:
    """A transport failure is a Bright Data request error, not a raw httpx one.

    An untyped ``httpx`` error escaping ``collect`` is none of the three types
    the fallback chain catches, so a Bright Data outage would hard-fail the
    Prefect task instead of falling the batch back to Gemini (#094).
    """

    @pytest.mark.parametrize(
        "error_type", _TRANSPORT_ERROR_TYPES, ids=lambda t: t.__name__
    )
    async def test_trigger_transport_failure_raises_request_error(
        self, mocker, error_type: type[httpx.TransportError]
    ) -> None:
        # Arrange
        _patch_settings(mocker)
        error = error_type("bright data is unreachable")
        _patch_failing_async_client(mocker, error)

        # Act & Assert
        with pytest.raises(BrightDataRequestError) as exc_info:
            await collect(
                DATASET_ID, INPUTS, timeout_seconds=60.0, poll_interval_seconds=1.0
            )

        message = str(exc_info.value)
        assert "api.brightdata.com/datasets/v3/trigger" in message
        assert "bright data is unreachable" in message
        assert exc_info.value.__cause__ is error

    @pytest.mark.parametrize(
        "error_type", _TRANSPORT_ERROR_TYPES, ids=lambda t: t.__name__
    )
    async def test_poll_transport_failure_raises_request_error(
        self, mocker, error_type: type[httpx.TransportError]
    ) -> None:
        # Arrange — the GET seam: the collection triggered, then the network died.
        _patch_settings(mocker)
        mocker.patch(
            "tree.data.web.web_scraper_api._post_json",
            new_callable=AsyncMock,
            return_value={"snapshot_id": "sd_1"},
        )
        error = error_type("bright data is unreachable")
        _patch_failing_async_client(mocker, error)

        # Act & Assert
        with pytest.raises(BrightDataRequestError) as exc_info:
            await collect(
                DATASET_ID, INPUTS, timeout_seconds=60.0, poll_interval_seconds=1.0
            )

        message = str(exc_info.value)
        assert "api.brightdata.com/datasets/v3/progress/sd_1" in message
        assert exc_info.value.__cause__ is error

    @pytest.mark.parametrize(
        "error",
        [
            TypeError("post() got an unexpected keyword argument"),
            AttributeError("'NoneType' object has no attribute 'get'"),
            httpx.InvalidURL("relative urls are not supported"),
        ],
        ids=["TypeError", "AttributeError", "InvalidURL"],
    )
    async def test_non_transport_failure_surfaces_as_itself(
        self, mocker, error: Exception
    ) -> None:
        # Arrange — a genuine bug on our side (including httpx's own non-transport
        # ``InvalidURL``) must NOT be mislabelled as a Bright Data request error.
        _patch_settings(mocker)
        _patch_failing_async_client(mocker, error)

        # Act & Assert
        with pytest.raises(type(error)):
            await collect(
                DATASET_ID, INPUTS, timeout_seconds=60.0, poll_interval_seconds=1.0
            )
