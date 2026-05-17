"""Unit tests for the WARNING that fires when ``app_config.models.embedding.dimensions``
disagrees with ``settings.embedding_dim``.

Phase 1 keeps the YAML override path open (local dev legitimately swaps
embedding models) but logs at load time so the mismatch is visible. The
hard error lives between ``settings.embedding_dim`` and the **live mongot
index**, not between settings and app_config. See
``tracker/016-pin-embedding-model-and-dim-in-settings.groomed.md``.
"""

from __future__ import annotations

import logging
import textwrap

import pytest


class TestAppConfigEmbeddingMismatchWarning:
    def test_warning_logged_when_yaml_dimensions_differ_from_settings(
        self,
        tmp_path,
        mocker,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Arrange — pin settings to 1024, YAML to 768.
        mocker.patch("tree.config.app_config.settings.embedding_dim", new=1024)

        custom = tmp_path / "custom.yaml"
        custom.write_text(
            textwrap.dedent("""\
                models:
                  embedding:
                    provider: sentence-transformers
                    model: all-MiniLM-L6-v2
                    dimensions: 768
            """)
        )

        # Act
        from tree.config.app_config import load_app_config

        with caplog.at_level(logging.WARNING, logger="tree.config.app_config"):
            config = load_app_config(custom)

        # Assert
        assert config.models.embedding.dimensions == 768

        warning_records = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "embedding" in record.getMessage().lower()
        ]
        assert len(warning_records) == 1, (
            f"expected exactly one embedding-dimension warning, got "
            f"{len(warning_records)}: {[r.getMessage() for r in warning_records]}"
        )
        message = warning_records[0].getMessage()
        assert "768" in message
        assert "1024" in message

    def test_no_warning_when_yaml_matches_settings(
        self,
        tmp_path,
        mocker,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Arrange — both at 1024.
        mocker.patch("tree.config.app_config.settings.embedding_dim", new=1024)

        custom = tmp_path / "custom.yaml"
        custom.write_text(
            textwrap.dedent("""\
                models:
                  embedding:
                    provider: voyage
                    model: voyage-3
                    dimensions: 1024
            """)
        )

        # Act
        from tree.config.app_config import load_app_config

        with caplog.at_level(logging.WARNING, logger="tree.config.app_config"):
            load_app_config(custom)

        # Assert — no embedding-dimension mismatch warning.
        warning_records = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "embedding" in record.getMessage().lower()
            and "dimensions" in record.getMessage().lower()
        ]
        assert warning_records == []
