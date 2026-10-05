"""``get_cloud_client()`` targets Horizon, where the gateway owns identity.

The client sends NO ``horizon-*`` header: the gateway injects the verified
``horizon-actor-email`` after Horizon authentication (and strips any
client-supplied one), so a header set here would at best be dropped.
"""

import pytest
from fastmcp.client.auth.oauth import OAuth

from tree.config.settings import settings
from tree.mcp import client as client_module


def test_cloud_client_authenticates_with_horizon_oauth(mocker) -> None:
    client_cls = mocker.patch.object(client_module, "Client")

    client_module.get_cloud_client()

    client_cls.assert_called_once_with(settings.tree_memory_cloud_url, auth="oauth")


@pytest.mark.filterwarnings("ignore:Using in-memory token storage:UserWarning")
def test_cloud_client_sends_no_horizon_header() -> None:
    transport = client_module.get_cloud_client().transport

    assert isinstance(transport.auth, OAuth)
    assert not [
        name for name in transport.headers if name.lower().startswith("horizon-")
    ]
