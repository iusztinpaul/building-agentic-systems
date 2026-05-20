import textwrap
from collections import Counter

from tree.config.app_config import (
    AppConfig,
    HuggingFaceDatasetSource,
    SubstackArticleSource,
    SubstackRssSource,
    WebSource,
    YouTubeRssSource,
    YouTubeVideoSource,
    load_app_config,
)


class TestLoadAppConfig:
    def test_loads_default_yaml(self):
        config = load_app_config()

        assert config.models.llm.provider == "gemini"
        # Bumped from gemini-2.5-flash-lite → gemini-3.1-flash-lite in
        # commit 210f8d5 (configs/default.yaml). The test asserts the
        # live YAML value so the unit suite stays green; bumping the
        # model in YAML requires a single matching change here.
        assert config.models.llm.model == "gemini-3.1-flash-lite"
        # default.yaml is now authoritative for embedding (#034). After
        # #038 the project consolidated on a single Voyage client backed
        # by the multimodal endpoint; ``voyage-multimodal-3`` is also
        # 1024-d, so the dim stays put while the model identifier
        # changes. #039 split the single ``embedding`` block into a
        # transient ``resolution_embedding`` and a persisted
        # ``search_embedding``; both still point at the same model/dim so
        # this task is behavior-preserving.
        assert config.models.resolution_embedding.provider == "voyage"
        assert config.models.resolution_embedding.model == "voyage-multimodal-3"
        assert config.models.resolution_embedding.dimensions == 1024
        assert config.models.search_embedding.provider == "voyage"
        assert config.models.search_embedding.model == "voyage-multimodal-3"
        assert config.models.search_embedding.dimensions == 1024
        # #044: real-time request-batching caps default to the Voyage
        # per-request limits for voyage-multimodal-3.
        assert config.models.embedding_batch.max_inputs == 1000
        assert config.models.embedding_batch.max_total_tokens == 320_000
        assert config.models.embedding_batch.max_input_tokens == 32_000
        assert config.extraction.chunk_size == 512
        assert config.extraction.llm_concurrency == 5

    def test_embedding_batch_defaults_when_absent(self, tmp_path):
        """A YAML with no ``models.embedding_batch`` block falls back to the
        typed defaults (the Voyage per-request caps) — #044."""

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
        assert config.models.embedding_batch.max_total_tokens == 320_000
        assert config.models.embedding_batch.max_input_tokens == 32_000

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

    def test_loads_default_yaml_sources_flat_shape(self):
        """default.yaml uses the flat ``sources:`` list shape (post-#007).

        Counts each variant; deeper per-variant assertions live in
        ``test_sources_config.py``.
        """

        config = load_app_config()

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

    def test_loads_default_yaml_huggingface_dataset_entry(self):
        """The HF arxiv entry preserves the parameters from the legacy YAML."""

        config = load_app_config()

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

    def test_loads_default_yaml_normalizes_untyped_to_web(self):
        """The two bare ``- uri:`` entries (Reddit, Anthropic) load as WebSource."""

        config = load_app_config()

        web_entries = [e for e in config.sources.sources if isinstance(e, WebSource)]
        assert len(web_entries) == 2
        web_uris = {e.uri for e in web_entries}
        assert any("reddit.com" in u for u in web_uris)
        assert any("anthropic.com" in u for u in web_uris)

    def test_default_yaml_round_trip_preserves_typed_variants(self):
        """Round-trip the default YAML: every entry is a typed Pydantic variant."""

        config = load_app_config()

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
        # resolution_embedding falls back to the EmbeddingConfig defaults.
        assert config.models.resolution_embedding.provider == "voyage"
        assert config.models.resolution_embedding.model == "voyage-multimodal-3"
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
