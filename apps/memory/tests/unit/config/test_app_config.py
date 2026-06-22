import textwrap
from collections import Counter

from tree.config.app_config import (
    AppConfig,
    ConcurrencyConfig,
    DreamConfig,
    HuggingFaceDatasetSource,
    SubstackArticleSource,
    SubstackRssSource,
    WebSource,
    YouTubeRssSource,
    YouTubeVideoSource,
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

    def test_loads_default_yaml_sources_flat_shape(self, frozen_config_path):
        """The config uses the flat ``sources:`` list shape (post-#007).

        Counts each variant; deeper per-variant assertions live in
        ``test_sources_config.py``.
        """

        config = load_app_config(frozen_config_path)

        counts = Counter(type(e).__name__ for e in config.sources.sources)

        assert counts == {
            "SubstackRssSource": 5,
            "SubstackArticleSource": 10,
            "HuggingFaceDatasetSource": 1,
            "WebSource": 2,
            "YouTubeRssSource": 1,
            "YouTubeVideoSource": 1,
        }
        assert sum(counts.values()) == 20

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

    def test_loads_default_yaml_huggingface_dataset_entry(self, frozen_config_path):
        """The HF arxiv entry preserves the parameters from the legacy YAML."""

        config = load_app_config(frozen_config_path)

        hf_entries = [
            e for e in config.sources.sources if isinstance(e, HuggingFaceDatasetSource)
        ]
        assert len(hf_entries) == 1
        entry = hf_entries[0]
        assert entry.uri == "librarian-bots/arxiv-metadata-snapshot"
        assert entry.max_samples == 10
        assert entry.fetch_content is False
        assert entry.batch_size == 50
        assert entry.concurrency == 10

    def test_loads_default_yaml_normalizes_untyped_to_web(self, frozen_config_path):
        """The two bare ``- uri:`` entries (Reddit, Anthropic) load as WebSource."""

        config = load_app_config(frozen_config_path)

        web_entries = [e for e in config.sources.sources if isinstance(e, WebSource)]
        assert len(web_entries) == 2
        web_uris = {e.uri for e in web_entries}
        assert any("reddit.com" in u for u in web_uris)
        assert any("anthropic.com" in u for u in web_uris)

    def test_default_yaml_round_trip_preserves_typed_variants(self, frozen_config_path):
        """Round-trip the config YAML: every entry is a typed Pydantic variant."""

        config = load_app_config(frozen_config_path)

        assert all(
            isinstance(
                s,
                (
                    SubstackRssSource,
                    SubstackArticleSource,
                    HuggingFaceDatasetSource,
                    WebSource,
                    YouTubeRssSource,
                    YouTubeVideoSource,
                ),
            )
            for s in config.sources.sources
        )

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
