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
        assert config.models.llm.model == "gemini-2.5-flash-lite"
        assert config.models.embedding.dimensions == 384
        assert config.extraction.chunk_size == 512
        assert config.extraction.llm_concurrency == 5

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
        assert config.models.embedding.dimensions == 768

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
