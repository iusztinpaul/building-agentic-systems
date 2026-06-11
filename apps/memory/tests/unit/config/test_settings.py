"""Unit tests for :class:`tree.config.settings.Settings`.

Post-#034 :class:`Settings` only carries credentials and infrastructure
endpoints. The exhaustive contract for the credentials-only surface
lives in :mod:`test_settings_credentials_only`; this module keeps a
small focused smoke test that the module loads and the Mongo URI helper
still resolves under the default env.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from tree.config.settings import MongoSettings


class TestSettingsSmoke:
    def test_settings_singleton_imports(self) -> None:
        # Arrange / Act
        from tree.config.settings import settings

        # Assert
        assert settings is not None
        # Mongo URI computed field is the load-bearing helper for every
        # init_mongodb() caller in the project. The operator env may point
        # at local Mongo ("mongodb") or Atlas ("mongodb+srv") — both are valid.
        uri = settings.mongo.mongo_uri.get_secret_value()
        assert uri.startswith(("mongodb://", "mongodb+srv://"))
        assert "@" in uri  # creds in URI


class TestMongoUriScheme:
    def test_scheme_field_defaults_to_local_mongodb(self) -> None:
        # Assert: the declared default targets the local replica set. Checked
        # on the field (not an instance) so the operator's MONGO_SCHEME env
        # cannot leak into the assertion.
        assert MongoSettings.model_fields["mongo_scheme"].default == "mongodb"

    def test_mongodb_scheme_builds_direct_connection_uri(self) -> None:
        # Arrange: scheme passed explicitly — instances read the operator's
        # env (e.g. MONGO_SCHEME=mongodb+srv with an Atlas .env) otherwise.
        mongo = MongoSettings(
            mongo_scheme="mongodb",
            mongo_host="localhost",
            mongo_port=27017,
            mongo_initdb_root_username="tree",
            mongo_initdb_root_password=SecretStr("tree"),
        )

        # Act
        uri = mongo.mongo_uri.get_secret_value()

        # Assert
        assert uri == (
            "mongodb://tree:tree@localhost:27017/?directConnection=true&authSource=admin"
        )

    def test_srv_scheme_builds_atlas_uri_without_port_or_direct_connection(
        self,
    ) -> None:
        # Arrange
        mongo = MongoSettings(
            mongo_scheme="mongodb+srv",
            mongo_host="tree.example.mongodb.net",
            mongo_port=27017,
            mongo_initdb_root_username="atlas_user",
            mongo_initdb_root_password=SecretStr("atlas_pwd"),
        )

        # Act
        uri = mongo.mongo_uri.get_secret_value()

        # Assert
        assert uri == (
            "mongodb+srv://atlas_user:atlas_pwd@tree.example.mongodb.net/?retryWrites=true&w=majority"
        )

    def test_unknown_scheme_is_rejected(self) -> None:
        # Arrange / Act / Assert
        with pytest.raises(ValidationError, match="mongo_scheme"):
            MongoSettings(mongo_scheme="postgres")
