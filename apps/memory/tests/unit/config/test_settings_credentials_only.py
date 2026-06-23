"""Unit tests for the post-#034 ``Settings`` contract.

After the YAML-as-source-of-truth migration, :class:`tree.config.settings.Settings`
carries credentials and infrastructure endpoints **only**. Behavior knobs
(embedding model/dim, dedup thresholds, etc.) live in
``apps/memory/configs/default.yaml`` and are typed by
:mod:`tree.config.app_config`.

These tests pin that contract from both directions:

1. ``Settings`` exposes exactly the credentials/infra surface — no
   ``dedup`` sub-config, no ``embedding_*`` fields.
2. ``app_config.extraction.dedup.supersession_candidate_cap`` defaults to
   ``8`` from the YAML (was env-only on the retired
   ``settings.DedupConfig``).
3. The documented escape hatch — ``TREE_EXTRACTION__DEDUP__<KEY>`` env
   overrides — still works.
4. The decommissioned ``DEDUP_*`` prefix is inert (silently ignored).

See ``tracker/034-voyage-3-yaml-default.groomed.md``.
"""

from __future__ import annotations

import importlib

import pytest


def _reload_settings_module():
    """Re-import ``tree.config.settings`` so env-var changes are picked up.

    ``pydantic-settings`` reads env vars at instantiation time and the
    module-level ``settings`` singleton is built at import time, so an
    env override only takes effect after reload.
    """

    import tree.config.settings as settings_module

    return importlib.reload(settings_module)


class TestSettingsCredentialsOnlySurface:
    """:class:`Settings` carries credentials + infra only.

    Pinning the surface guards against future drift: any reintroduction
    of a behavior knob on ``Settings`` would re-open the
    two-sources-of-truth bug #034 was filed to close.
    """

    def test_settings_does_not_expose_dedup(self) -> None:
        # Arrange / Act
        module = _reload_settings_module()

        # Assert — the retired ``DedupConfig`` field is gone.
        assert not hasattr(module.settings, "dedup")

    def test_settings_does_not_expose_embedding_fields(self) -> None:
        # Arrange / Act
        module = _reload_settings_module()

        # Assert — the retired pin fields are gone; YAML is authoritative.
        assert not hasattr(module.settings, "embedding_provider")
        assert not hasattr(module.settings, "embedding_model")
        assert not hasattr(module.settings, "embedding_dim")

    def test_settings_keeps_credentials_and_infra_fields(self) -> None:
        # Arrange / Act
        module = _reload_settings_module()
        s = module.settings

        # Assert — credentials + infra survive the migration.
        assert hasattr(s, "mongo")
        assert hasattr(s.mongo, "mongo_host")
        assert hasattr(s.mongo, "mongo_initdb_database")
        assert hasattr(s, "google_api_key")
        assert hasattr(s, "voyage_api_key")
        assert hasattr(s, "modal_embedding_api_key")
        assert hasattr(s, "brightdata_api_key")
        assert hasattr(s, "brightdata_unlocker_zone")
        assert hasattr(s, "brightdata_serp_zone")

    def test_settings_surface_is_locked_down(self) -> None:
        """The Settings model exposes a closed set of fields.

        Future maintainers who try to add ``Settings.something_threshold``
        will fail this assertion and be redirected to the YAML.
        """

        # Arrange / Act
        module = _reload_settings_module()
        # ``model_fields`` is the canonical Pydantic surface declaration.
        fields = set(module.Settings.model_fields.keys())

        # Assert — exactly the credential + infra fields, nothing else.
        # Opik credentials (api key, workspace, project name) are infra/creds
        # too — they belong here, not in the YAML behavior config.
        assert fields == {
            "mongo",
            "google_api_key",
            "voyage_api_key",
            "modal_embedding_api_key",
            "brightdata_api_key",
            "brightdata_unlocker_zone",
            "brightdata_serp_zone",
            "opik_api_key",
            "opik_workspace",
            "opik_project_name",
            # Prefect Horizon (FastMCP Cloud) deployment endpoint + the
            # serverless fast-boot toggle — infra config, not behaviour knobs.
            "tree_memory_cloud_url",
            "mcp_skip_index_bootstrap",
            # Ephemeral ``.tree/`` working-dir location (path/infra, not a
            # behaviour knob) — ``/tmp/.tree`` on the read-only serverless host.
            "tree_working_dir",
        }


class TestAppConfigDedupSupersessionCandidateCap:
    """The ``supersession_candidate_cap`` knob migrated from
    ``settings.DedupConfig`` into ``app_config.extraction.dedup`` per
    #034. Default must remain ``8``.
    """

    def test_default_is_eight(self, frozen_config_path) -> None:
        # Arrange / Act
        from tree.config.app_config import load_app_config

        config = load_app_config(frozen_config_path)

        # Assert
        assert config.extraction.dedup.supersession_candidate_cap == 8


class TestTreeEnvOverrideEscapeHatch:
    """The ``TREE_<SECTION>__<KEY>`` escape hatch is the canonical way
    operators override a single YAML knob from the env — the only
    behavior knob path that still lives in env vars."""

    def test_dedup_auto_merge_threshold_override(
        self, monkeypatch: pytest.MonkeyPatch, frozen_config_path
    ) -> None:
        # Arrange
        monkeypatch.setenv("TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD", "0.99")

        # Act
        from tree.config.app_config import load_app_config

        config = load_app_config(frozen_config_path)

        # Assert
        assert config.extraction.dedup.auto_merge_threshold == 0.99

    def test_dedup_supersession_candidate_cap_override(
        self, monkeypatch: pytest.MonkeyPatch, frozen_config_path
    ) -> None:
        # Arrange
        monkeypatch.setenv("TREE_EXTRACTION__DEDUP__SUPERSESSION_CANDIDATE_CAP", "4")

        # Act
        from tree.config.app_config import load_app_config

        config = load_app_config(frozen_config_path)

        # Assert
        assert config.extraction.dedup.supersession_candidate_cap == 4


class TestDecommissionedDedupPrefixIsInert:
    """The ``DEDUP_*`` env-var prefix that used to drive
    ``settings.DedupConfig`` is decommissioned. Setting it must NOT
    silently override the YAML — operators who still set it will see
    YAML defaults, exactly as the migration note in the task warns.

    This protects the operator from a worse failure mode: the legacy
    var SILENTLY taking effect under a partial rollback.

    We use :func:`load_app_config` (the public re-loader) rather than
    ``importlib.reload`` so we don't mutate the module-level
    ``tree.config.app_config.app_config`` singleton and poison
    downstream tests in the same session.
    """

    def test_legacy_dedup_auto_merge_threshold_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, frozen_config_path
    ) -> None:
        # Arrange
        monkeypatch.setenv("DEDUP_AUTO_MERGE_THRESHOLD", "0.97")

        # Act
        from tree.config.app_config import load_app_config

        config = load_app_config(frozen_config_path)

        # Assert — YAML value wins (0.95), NOT 0.97.
        assert config.extraction.dedup.auto_merge_threshold == 0.95

    def test_legacy_dedup_supersession_candidate_cap_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, frozen_config_path
    ) -> None:
        # Arrange
        monkeypatch.setenv("DEDUP_SUPERSESSION_CANDIDATE_CAP", "4")

        # Act
        from tree.config.app_config import load_app_config

        config = load_app_config(frozen_config_path)

        # Assert — YAML value wins (8), NOT 4.
        assert config.extraction.dedup.supersession_candidate_cap == 8
