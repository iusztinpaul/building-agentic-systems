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


# ---------------------------------------------------------------------------
# #032 — DedupConfig
# ---------------------------------------------------------------------------


class TestDedupConfigDefaults:
    """Per ``plan.md:524-530`` the dedup config exposes four knobs with
    pinned defaults the rest of the resolver/dedup pipeline reads."""

    def test_defaults(self) -> None:
        module = _reload_settings_module()

        cfg = module.settings.dedup
        assert cfg.auto_merge_threshold == 0.95
        assert cfg.flag_threshold == 0.85
        assert cfg.fuzzy_threshold == 90
        assert cfg.match_same_type_only is True
        # #032 fix-1: bound-candidate-set cap on the supersession
        # resolver's judge calls. Default 8 keeps LLM cost bounded.
        assert cfg.supersession_candidate_cap == 8


class TestDedupConfigEnvOverrides:
    def test_auto_merge_threshold_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEDUP_AUTO_MERGE_THRESHOLD", "0.99")

        module = _reload_settings_module()

        assert module.settings.dedup.auto_merge_threshold == 0.99

    def test_flag_threshold_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEDUP_FLAG_THRESHOLD", "0.70")

        module = _reload_settings_module()

        assert module.settings.dedup.flag_threshold == 0.70

    def test_fuzzy_threshold_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEDUP_FUZZY_THRESHOLD", "85")

        module = _reload_settings_module()

        assert module.settings.dedup.fuzzy_threshold == 85

    def test_match_same_type_only_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEDUP_MATCH_SAME_TYPE_ONLY", "false")

        module = _reload_settings_module()

        assert module.settings.dedup.match_same_type_only is False

    def test_supersession_candidate_cap_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEDUP_SUPERSESSION_CANDIDATE_CAP", "4")

        module = _reload_settings_module()

        assert module.settings.dedup.supersession_candidate_cap == 4
