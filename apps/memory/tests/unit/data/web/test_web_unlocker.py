"""Unit tests for tree.data.web.web_unlocker.fetch_url."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
    fetch_url,
)


def _patch_settings(
    mocker,
    *,
    api_key: str = "test-api-key",
    zone: str = "test-zone",
) -> None:
    """Patch the settings singleton in the web_unlocker module."""

    fake_secret = MagicMock()
    fake_secret.get_secret_value.return_value = api_key

    fake_settings = MagicMock()
    fake_settings.brightdata_api_key = fake_secret
    fake_settings.brightdata_unlocker_zone = zone

    mocker.patch("tree.data.web.web_unlocker.settings", fake_settings)


def _build_response(
    *, status_code: int, text: str = "", request_url: str = "https://example.com"
) -> httpx.Response:
    """Build an httpx.Response object suitable as an AsyncClient.post return value."""

    request = httpx.Request("POST", "https://api.brightdata.com/request")
    return httpx.Response(status_code=status_code, text=text, request=request)


class TestFetchUrlConfiguration:
    async def test_raises_configuration_error_when_api_key_empty(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker, api_key="", zone="some-zone")

        # Act & Assert
        with pytest.raises(BrightDataConfigurationError, match="BRIGHTDATA_API_KEY"):
            await fetch_url("https://example.com")

    async def test_raises_configuration_error_when_zone_empty(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker, api_key="key", zone="")

        # Act & Assert
        with pytest.raises(
            BrightDataConfigurationError, match="BRIGHTDATA_UNLOCKER_ZONE"
        ):
            await fetch_url("https://example.com")


class TestFetchUrlValidation:
    @pytest.mark.parametrize(
        "bad_url",
        ["", "ftp://example.com", "example.com", "   ", "javascript:void(0)"],
        ids=["empty", "ftp", "no-scheme", "whitespace", "javascript"],
    )
    async def test_raises_value_error_for_bad_url(self, mocker, bad_url: str) -> None:
        # Arrange
        _patch_settings(mocker)

        # Act & Assert
        with pytest.raises(ValueError, match="http"):
            await fetch_url(bad_url)


class TestFetchUrlHttpBehavior:
    async def test_returns_response_body_verbatim_on_200(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        body_text = "# Heading\n\nSome **markdown** body."
        mock_response = _build_response(status_code=200, text=body_text)

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        mock_client_cm = MagicMock()
        mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cm.__aexit__ = AsyncMock(return_value=None)

        mocker.patch(
            "tree.data.web.web_unlocker.httpx.AsyncClient",
            return_value=mock_client_cm,
        )

        # Act
        result = await fetch_url("https://example.com/page")

        # Assert
        assert result == body_text

    @pytest.mark.parametrize(
        "status_code",
        [400, 401, 403, 404, 429, 500, 502, 503],
        ids=["400", "401", "403", "404", "429", "500", "502", "503"],
    )
    async def test_raises_request_error_on_non_2xx(
        self, mocker, status_code: int
    ) -> None:
        # Arrange
        _patch_settings(mocker)
        mock_response = _build_response(status_code=status_code, text="boom")

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        mock_client_cm = MagicMock()
        mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cm.__aexit__ = AsyncMock(return_value=None)

        mocker.patch(
            "tree.data.web.web_unlocker.httpx.AsyncClient",
            return_value=mock_client_cm,
        )

        # Act & Assert
        with pytest.raises(BrightDataRequestError, match=str(status_code)):
            await fetch_url("https://example.com")

    async def test_posts_expected_request_body_and_headers(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker, api_key="my-key", zone="my-zone")
        mock_response = _build_response(status_code=200, text="ok")

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        mock_client_cm = MagicMock()
        mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cm.__aexit__ = AsyncMock(return_value=None)

        mocker.patch(
            "tree.data.web.web_unlocker.httpx.AsyncClient",
            return_value=mock_client_cm,
        )

        target_url = "https://example.com/article"

        # Act
        await fetch_url(target_url)

        # Assert
        mock_client.post.assert_awaited_once()
        call_args = mock_client.post.call_args
        assert call_args.args[0] == "https://api.brightdata.com/request"
        assert call_args.kwargs["json"] == {
            "zone": "my-zone",
            "url": target_url,
            "format": "raw",
            "data_format": "markdown",
        }
        assert call_args.kwargs["headers"]["Authorization"] == "Bearer my-key"

    async def test_html_data_format_passed_through(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker, api_key="k", zone="z")
        mock_response = _build_response(status_code=200, text="<html></html>")

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        mock_client_cm = MagicMock()
        mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cm.__aexit__ = AsyncMock(return_value=None)

        mocker.patch(
            "tree.data.web.web_unlocker.httpx.AsyncClient",
            return_value=mock_client_cm,
        )

        # Act
        result = await fetch_url("https://example.com", data_format="html")

        # Assert
        assert result == "<html></html>"
        body = mock_client.post.call_args.kwargs["json"]
        assert body["data_format"] == "html"
        assert body["format"] == "raw"
