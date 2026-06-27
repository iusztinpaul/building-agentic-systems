import json
import textwrap

import pytest
from pydantic import TypeAdapter, ValidationError

from tree.config.app_config import (
    _DEFAULT_CONFIG_PATH,
    AppConfig,
    ConcurrencyConfig,
    DreamConfig,
    HuggingFaceDatasetSource,
    SourceEntry,
    load_app_config,
)


class TestLoadAppConfig:
    def test_loads_default_yaml(self, frozen_config_path):
        config = load_app_config(frozen_config_path)

        assert config.models.llm.provider == "gemini"
        assert config.models.llm.model == "gemini-3.1-flash-lite"
        # #048 flipped the default from the multimodal ``voyage-multimodal-3`` to
        # the TEXT model ``voyage-3.5`` (routed to /v1/embeddings); ``voyage-3.5``
        # is also 1024-d, so the dim stays put. #039 split the single
        # ``embedding`` block into a transient ``resolution_embedding`` and a
        # persisted ``search_embedding``; both point at the same model/dim.
        assert config.models.resolution_embedding.provider == "voyage"
        assert config.models.resolution_embedding.model == "voyage-3.5"
        assert config.models.resolution_embedding.dimensions == 1024
        assert config.models.search_embedding.provider == "voyage"
        assert config.models.search_embedding.model == "voyage-3.5"
        assert config.models.search_embedding.dimensions == 1024
        # #044: real-time request-batching caps. #054/ADR-002 dropped
        # max_total_tokens 320_000 → 10_000 (the shared free-tier Voyage TPM
        # window) and added dispatch_concurrency.
        assert config.models.embedding_batch.max_inputs == 1000
        assert config.models.embedding_batch.max_total_tokens == 10_000
        assert config.models.embedding_batch.max_input_tokens == 32_000
        assert config.models.embedding_batch.dispatch_concurrency == 1
        assert config.extraction.chunk_size == 512
        assert config.extraction.llm_concurrency == 5
        # #054: intra-run fan-out knobs.
        assert config.extraction.doc_concurrency == 1
        assert config.extraction.dedup_concurrency == 8

    def test_embedding_batch_defaults_when_absent(self, tmp_path):
        """A YAML with no ``models.embedding_batch`` block falls back to the
        typed defaults — #044, with the #054/ADR-002 max_total_tokens drop."""

        custom = tmp_path / "no_batch.yaml"
        custom.write_text(
            "models:\n"
            "  search_embedding:\n"
            "    provider: voyage\n"
            "    model: voyage-multimodal-3\n"
            "    dimensions: 1024\n"
        )

        config = load_app_config(custom)

        assert config.models.embedding_batch.max_inputs == 1000
        assert config.models.embedding_batch.max_total_tokens == 10_000
        assert config.models.embedding_batch.max_input_tokens == 32_000
        assert config.models.embedding_batch.dispatch_concurrency == 1

    def test_embedding_batch_caps_loaded_from_yaml(self, tmp_path):
        """Operator-tuned batching caps in YAML are read into the typed
        :class:`EmbeddingBatchConfig` (#044)."""

        custom = tmp_path / "batch.yaml"
        custom.write_text(
            "models:\n"
            "  embedding_batch:\n"
            "    max_inputs: 128\n"
            "    max_total_tokens: 50000\n"
            "    max_input_tokens: 8000\n"
        )

        config = load_app_config(custom)

        assert config.models.embedding_batch.max_inputs == 128
        assert config.models.embedding_batch.max_total_tokens == 50_000
        assert config.models.embedding_batch.max_input_tokens == 8000

    def test_dream_block_loaded_from_default_yaml(self, frozen_config_path):
        """The #051 ``dream:`` block is read into the typed :class:`DreamConfig`.

        Thresholds are NOT duplicated here — they stay in
        ``extraction.dedup``; ``DreamConfig`` carries no threshold field.
        """

        config = load_app_config(frozen_config_path)

        assert config.dream.enabled is True
        assert config.dream.cron == "0 4 * * *"
        # dry_run: true (report-only rollout), agreeing with the safer Pydantic
        # model default.
        assert config.dream.dry_run is True
        assert config.dream.max_pairs == 10_000
        assert config.dream.enable_supersession_judge is False

    def test_prefect_deploy_optional_false_in_default_yaml(self, frozen_config_path):
        """``prefect.deploy_optional`` is false in default.yaml (free-tier safe)."""

        config = load_app_config(frozen_config_path)

        assert config.prefect.deploy_optional is False

    def test_prefect_deploy_optional_defaults_false_when_absent(self, tmp_path):
        """A YAML with no ``prefect`` block defaults ``deploy_optional`` to false."""

        custom = tmp_path / "no_prefect.yaml"
        custom.write_text("query:\n  top_k: 5\n")

        config = load_app_config(custom)

        assert config.prefect.deploy_optional is False

    def test_prefect_deploy_optional_loaded_from_yaml(self, tmp_path):
        """An operator can opt into the optional deployments via YAML."""

        custom = tmp_path / "prefect.yaml"
        custom.write_text("prefect:\n  deploy_optional: true\n")

        config = load_app_config(custom)

        assert config.prefect.deploy_optional is True

    def test_dream_defaults_when_absent(self, tmp_path):
        """A YAML with no ``dream`` block falls back to the typed defaults."""

        custom = tmp_path / "no_dream.yaml"
        custom.write_text("query:\n  top_k: 5\n")

        config = load_app_config(custom)

        assert config.dream == DreamConfig()
        assert config.dream.enabled is True
        assert config.dream.dry_run is True  # safe model default
        assert config.dream.max_pairs == 10_000
        assert config.dream.enable_supersession_judge is False

    def test_dream_block_loaded_from_custom_yaml(self, tmp_path):
        """Operator-tuned dream knobs in YAML are read into the typed model."""

        custom = tmp_path / "dream.yaml"
        custom.write_text(
            "dream:\n"
            "  enabled: false\n"
            '  cron: "30 2 * * *"\n'
            "  dry_run: true\n"
            "  max_pairs: 42\n"
            "  enable_supersession_judge: true\n"
        )

        config = load_app_config(custom)

        assert config.dream.enabled is False
        assert config.dream.cron == "30 2 * * *"
        assert config.dream.dry_run is True
        assert config.dream.max_pairs == 42
        assert config.dream.enable_supersession_judge is True

    def test_app_config_has_no_sources_field(self):
        """``AppConfig`` is static memory config only — sources moved out to the
        repo-root ``sources/`` files (ADR-003), so the field is gone."""

        assert "sources" not in AppConfig.model_fields

    def test_load_app_config_ignores_a_legacy_sources_block(self, tmp_path):
        """A YAML that still carries a top-level ``sources:`` block loads green —
        the loader no longer eagerly validates it, and no ``sources`` attribute
        is materialised on the config."""

        custom = tmp_path / "with_sources.yaml"
        custom.write_text(
            textwrap.dedent("""\
                sources:
                  - uri: https://example.substack.com/feed
                    type: substack_rss
                query:
                  top_k: 5
            """)
        )

        config = load_app_config(custom)

        assert config.query.top_k == 5
        assert not hasattr(config, "sources")

    def test_loads_custom_yaml(self, tmp_path):
        custom = tmp_path / "custom.yaml"
        custom.write_text(
            textwrap.dedent("""\
                models:
                  llm:
                    model: gemini-2.0-flash
                extraction:
                  chunk_size: 256
                  resolution:
                    fuzzy_threshold: 0.9
            """)
        )

        config = load_app_config(custom)

        assert config.models.llm.model == "gemini-2.0-flash"
        assert config.models.llm.provider == "gemini"
        assert config.extraction.chunk_size == 256
        assert config.extraction.resolution.fuzzy_threshold == 0.9
        # Unset values keep defaults.
        assert config.extraction.chunk_overlap == 64
        # Embedding dimensions fall back to the plain Pydantic default
        # (1024) when the custom YAML doesn't override them (#034/#039).
        assert config.models.resolution_embedding.dimensions == 1024
        assert config.models.search_embedding.dimensions == 1024

    def test_search_embedding_only_defaults_resolution_embedding(self, tmp_path):
        """A YAML that sets only ``search_embedding`` loads cleanly with
        ``resolution_embedding`` falling back to the ``EmbeddingConfig``
        defaults (#039). Neither block is required; both default
        independently.
        """

        custom = tmp_path / "search_only.yaml"
        custom.write_text(
            textwrap.dedent("""\
                models:
                  search_embedding:
                    provider: voyage
                    model: voyage-multimodal-3
                    dimensions: 1024
            """)
        )

        config = load_app_config(custom)

        # search_embedding takes the explicit YAML values.
        assert config.models.search_embedding.provider == "voyage"
        assert config.models.search_embedding.model == "voyage-multimodal-3"
        assert config.models.search_embedding.dimensions == 1024
        # resolution_embedding falls back to the EmbeddingConfig defaults
        # (#048 flipped the code-level default model to the text ``voyage-3.5``).
        assert config.models.resolution_embedding.provider == "voyage"
        assert config.models.resolution_embedding.model == "voyage-3.5"
        assert config.models.resolution_embedding.dimensions == 1024

    def test_missing_file_returns_defaults(self, tmp_path):
        config = load_app_config(tmp_path / "nonexistent.yaml")

        assert config == AppConfig()

    def test_empty_yaml_returns_defaults(self, tmp_path):
        empty = tmp_path / "empty.yaml"
        empty.write_text("")

        config = load_app_config(empty)

        assert config == AppConfig()

    def test_env_var_override(self, tmp_path, monkeypatch):
        custom = tmp_path / "env.yaml"
        custom.write_text("extraction:\n  chunk_size: 1024\n")
        monkeypatch.setenv("APP_CONFIG_PATH", str(custom))

        config = load_app_config()

        assert config.extraction.chunk_size == 1024


class TestConcurrencyConfig:
    """#054 / ADR-002: the top-level ``concurrency:`` block."""

    def test_concurrency_block_loaded_from_default_yaml(self, frozen_config_path):
        """The concurrency knobs are read from YAML into the
        typed :class:`ConcurrencyConfig`."""

        config = load_app_config(frozen_config_path)

        assert config.concurrency.voyage_rpm == 3
        assert config.concurrency.voyage_tpm == 10_000
        assert config.concurrency.runner_global_limit == 4

    def test_concurrency_defaults_when_absent(self, tmp_path):
        """A YAML with no ``concurrency`` block falls back to the typed
        defaults."""

        custom = tmp_path / "no_concurrency.yaml"
        custom.write_text("query:\n  top_k: 5\n")

        config = load_app_config(custom)

        assert config.concurrency == ConcurrencyConfig()
        assert config.concurrency.voyage_rpm == 3
        assert config.concurrency.voyage_tpm == 10_000
        assert config.concurrency.runner_global_limit == 4

    def test_concurrency_block_loaded_from_custom_yaml(self, tmp_path):
        """Operator-tuned concurrency knobs in YAML are read into the typed
        model."""

        custom = tmp_path / "concurrency.yaml"
        custom.write_text(
            "concurrency:\n"
            "  voyage_rpm: 60\n"
            "  voyage_tpm: 1000000\n"
            "  runner_global_limit: 8\n"
        )

        config = load_app_config(custom)

        assert config.concurrency.voyage_rpm == 60
        assert config.concurrency.voyage_tpm == 1_000_000
        assert config.concurrency.runner_global_limit == 8


class TestHuggingFaceWindowFields:
    """#070: the HF offset-window fields ``num_workers`` (YAML-authored fan-out
    width) and ``offset`` (dispatch-time runtime coordinate, never in YAML)."""

    def test_defaults_preserve_todays_behavior(self):
        """A HF source built with only a ``uri`` has ``num_workers == 1`` and
        ``offset is None`` — a single whole-``max_samples`` window, no skip."""

        entry = HuggingFaceDatasetSource(uri="librarian-bots/arxiv-metadata-snapshot")

        assert entry.num_workers == 1
        assert entry.offset is None

    def test_explicit_values_are_carried(self):
        """An explicitly-constructed HF source carries the exact authored
        fan-out width and runtime offset."""

        entry = HuggingFaceDatasetSource(
            uri="librarian-bots/arxiv-metadata-snapshot",
            num_workers=4,
            offset=500,
        )

        assert entry.num_workers == 4
        assert entry.offset == 500

    def test_num_workers_must_be_at_least_one(self):
        """``num_workers`` is a fan-out width; values below 1 are rejected."""

        with pytest.raises(ValidationError):
            HuggingFaceDatasetSource(
                uri="librarian-bots/arxiv-metadata-snapshot", num_workers=0
            )

    def test_offset_defaults_to_none_in_yaml_authored_entry(self):
        """An operator may author ``num_workers`` in YAML; ``offset`` stays
        ``None`` because it is never authored — only set at dispatch."""

        entry = HuggingFaceDatasetSource.model_validate(
            {
                "type": "huggingface_dataset",
                "uri": "librarian-bots/arxiv-metadata-snapshot",
                "num_workers": 4,
            }
        )

        assert entry.num_workers == 4
        assert entry.offset is None

    def test_runtime_offset_set_via_model_copy(self):
        """#072 sets ``offset`` ONLY at dispatch via
        ``entry.model_copy(update={"offset": ...})`` — the authored entry is
        left untouched."""

        authored = HuggingFaceDatasetSource(
            uri="librarian-bots/arxiv-metadata-snapshot", num_workers=4
        )

        dispatched = authored.model_copy(update={"offset": 250})

        assert authored.offset is None
        assert dispatched.offset == 250
        assert dispatched.num_workers == 4

    def test_discriminated_union_round_trip_preserves_window_fields(self):
        """``model_dump()`` → JSON → ``TypeAdapter(list[SourceEntry])`` preserves
        the new window fields — the round-trip the orchestrator dispatches
        through ``run_deployment`` flow-run params. Covers both the ``None``
        and a set-int offset case."""

        adapter: TypeAdapter[list[SourceEntry]] = TypeAdapter(list[SourceEntry])
        entries = [
            HuggingFaceDatasetSource(
                uri="librarian-bots/arxiv-metadata-snapshot",
                max_samples=1000,
                fetch_content=True,
                batch_size=64,
                concurrency=8,
                num_workers=4,
                offset=None,
            ),
            HuggingFaceDatasetSource(
                uri="librarian-bots/arxiv-metadata-snapshot",
                max_samples=1000,
                num_workers=4,
                offset=250,
            ),
        ]

        serialized = json.loads(json.dumps([e.model_dump() for e in entries]))
        reparsed = adapter.validate_python(serialized)

        assert all(isinstance(e, HuggingFaceDatasetSource) for e in reparsed)

        first = reparsed[0]
        assert first.type == "huggingface_dataset"
        assert first.uri == "librarian-bots/arxiv-metadata-snapshot"
        assert first.max_samples == 1000
        assert first.fetch_content is True
        assert first.batch_size == 64
        assert first.concurrency == 8
        assert first.num_workers == 4
        assert first.offset is None

        second = reparsed[1]
        assert second.num_workers == 4
        assert second.offset == 250


class TestRunnerGlobalLimitBump:
    """#070: ``runner_global_limit`` is raised 4→6 in ``default.yaml`` ONLY; the
    typed default on :class:`ConcurrencyConfig` stays at 4."""

    def test_default_yaml_raises_runner_global_limit_to_six(self):
        """The real, human-tuned ``configs/default.yaml`` admits up to 6 runs."""

        config = load_app_config(_DEFAULT_CONFIG_PATH)

        assert config.concurrency.runner_global_limit == 6

    def test_typed_default_runner_global_limit_unchanged(self):
        """A bare ``ConcurrencyConfig()`` (no YAML) still reports 4 — the bump is
        YAML-only, so configs that omit the block are unchanged."""

        assert ConcurrencyConfig().runner_global_limit == 4


class TestExtractionConcurrencyKnobs:
    """#054: the new intra-run fan-out knobs on ``extraction`` +
    ``models.embedding_batch``, including the env-override hatch."""

    def test_extraction_fanout_knobs_loaded_from_default_yaml(self, frozen_config_path):
        config = load_app_config(frozen_config_path)

        assert config.extraction.doc_concurrency == 1
        assert config.extraction.dedup_concurrency == 8

    def test_dispatch_concurrency_loaded_from_default_yaml(self, frozen_config_path):
        config = load_app_config(frozen_config_path)

        assert config.models.embedding_batch.dispatch_concurrency == 1
        assert config.models.embedding_batch.max_total_tokens == 10_000

    def test_dedup_concurrency_env_override(self, tmp_path, monkeypatch):
        """``TREE_EXTRACTION__DEDUP_CONCURRENCY=4`` overrides the YAML default
        (8) — proves the existing override hatch reaches the new knob."""

        custom = tmp_path / "extraction.yaml"
        custom.write_text("extraction:\n  dedup_concurrency: 8\n")
        monkeypatch.setenv("TREE_EXTRACTION__DEDUP_CONCURRENCY", "4")

        config = load_app_config(custom)

        assert config.extraction.dedup_concurrency == 4

    def test_doc_concurrency_env_override(self, tmp_path, monkeypatch):
        """The override hatch also reaches ``doc_concurrency``."""

        custom = tmp_path / "extraction.yaml"
        custom.write_text("extraction:\n  doc_concurrency: 1\n")
        monkeypatch.setenv("TREE_EXTRACTION__DOC_CONCURRENCY", "3")

        config = load_app_config(custom)

        assert config.extraction.doc_concurrency == 3
