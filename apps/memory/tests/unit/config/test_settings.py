"""Unit tests for :class:`tree.config.settings.Settings`.

Post-#034 :class:`Settings` only carries credentials and infrastructure
endpoints. The exhaustive contract for the credentials-only surface
lives in :mod:`test_settings_credentials_only`; this module keeps a
small focused smoke test that the module loads and the Mongo URI helper
still resolves under the default env.
"""

from __future__ import annotations


class TestSettingsSmoke:
    def test_settings_singleton_imports(self) -> None:
        # Arrange / Act
        from tree.config.settings import settings

        # Assert
        assert settings is not None
        # Mongo URI computed field is the load-bearing helper for every
        # init_mongodb() caller in the project.
        uri = settings.mongo.mongo_uri.get_secret_value()
        assert uri.startswith("mongodb://")
        assert "@" in uri  # creds in URI
