"""Unit tests for the embedding-related fields on :class:`tree.config.settings.Settings`.

Phase 1 of multi-tenancy pins the embedding model identifier and the vector
dimension to ``settings.py`` so the indexing pipeline, the mongot
vector-search index, and any future validation all read from a single
source of truth. See ``tracker/016-pin-embedding-model-and-dim-in-settings.groomed.md``.
"""

from __future__ import annotations

import importlib

import pytest


def _reload_settings_module():
    """Re-import ``tree.config.settings`` so env-var changes are picked up.

    ``pydantic-settings`` reads env vars at instantiation time, and the
    module-level ``settings`` singleton is built at import time. To exercise
    env-var override paths we have to re-import the module.
    """

    import tree.config.settings as settings_module

    return importlib.reload(settings_module)


class TestEmbeddingDefaults:
    def test_default_embedding_provider_is_voyage(self) -> None:
        # Arrange / Act
        module = _reload_settings_module()

        # Assert
        assert module.settings.embedding_provider == "voyage"

    def test_default_embedding_model_is_voyage_3(self) -> None:
        # Arrange / Act
        module = _reload_settings_module()

        # Assert
        assert module.settings.embedding_model == "voyage-3"

    def test_default_embedding_dim_is_1024(self) -> None:
        # Arrange / Act
        module = _reload_settings_module()

        # Assert
        assert module.settings.embedding_dim == 1024


class TestEmbeddingEnvOverrides:
    def test_embedding_dim_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setenv("EMBEDDING_DIM", "384")

        # Act
        module = _reload_settings_module()

        # Assert
        assert module.settings.embedding_dim == 384

    def test_embedding_model_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")

        # Act
        module = _reload_settings_module()

        # Assert
        assert module.settings.embedding_model == "all-MiniLM-L6-v2"

    def test_embedding_provider_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setenv("EMBEDDING_PROVIDER", "sentence-transformers")

        # Act
        module = _reload_settings_module()

        # Assert
        assert module.settings.embedding_provider == "sentence-transformers"


class TestSettingsSingleton:
    def test_settings_singleton_exposes_embedding_fields(self) -> None:
        # Arrange / Act
        from tree.config.settings import settings

        # Assert
        assert hasattr(settings, "embedding_provider")
        assert hasattr(settings, "embedding_model")
        assert hasattr(settings, "embedding_dim")
        assert isinstance(settings.embedding_dim, int)
        assert isinstance(settings.embedding_model, str)
        assert isinstance(settings.embedding_provider, str)
