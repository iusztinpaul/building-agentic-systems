"""Unit tests for the discriminated-union ``SourcesConfig`` schema."""

import textwrap

import pytest
from pydantic import ValidationError

from tree.config.app_config import (
    HuggingFaceDatasetSource,
    SourcesConfig,
    SubstackArticleSource,
    SubstackRssSource,
    WebSource,
    YouTubeRssSource,
    YouTubeVideoSource,
    load_app_config,
)


class TestVariantValidation:
    """Each variant validates its required fields and discriminator literal."""

    def test_substack_rss_validates(self):
        config = SourcesConfig.model_validate(
            {
                "sources": [
                    {
                        "type": "substack_rss",
                        "uri": "https://example.substack.com/feed",
                    },
                ],
            }
        )

        assert len(config.sources) == 1
        assert isinstance(config.sources[0], SubstackRssSource)
        assert config.sources[0].uri == "https://example.substack.com/feed"
        assert config.sources[0].type == "substack_rss"

    def test_substack_article_validates(self):
        config = SourcesConfig.model_validate(
            {
                "sources": [
                    {
                        "type": "substack_article",
                        "uri": "https://example.substack.com/p/title",
                    },
                ],
            }
        )

        assert isinstance(config.sources[0], SubstackArticleSource)
        assert config.sources[0].uri == "https://example.substack.com/p/title"

    def test_web_validates(self):
        config = SourcesConfig.model_validate(
            {
                "sources": [
                    {"type": "web", "uri": "https://news.ycombinator.com"},
                ],
            }
        )

        assert isinstance(config.sources[0], WebSource)
        assert config.sources[0].uri == "https://news.ycombinator.com"

    def test_youtube_video_validates(self):
        config = SourcesConfig.model_validate(
            {
                "sources": [
                    {
                        "type": "youtube_video",
                        "uri": "https://www.youtube.com/watch?v=eYaWxljC4sA",
                    },
                ],
            }
        )

        assert isinstance(config.sources[0], YouTubeVideoSource)
        assert config.sources[0].type == "youtube_video"
        assert config.sources[0].uri == "https://www.youtube.com/watch?v=eYaWxljC4sA"

    def test_youtube_rss_validates(self):
        config = SourcesConfig.model_validate(
            {
                "sources": [
                    {
                        "type": "youtube_rss",
                        "uri": "https://www.youtube.com/feeds/videos.xml?channel_id=UCkyHDwRWMEluOEYmOGJ_2nw",
                    },
                ],
            }
        )

        assert isinstance(config.sources[0], YouTubeRssSource)
        assert config.sources[0].type == "youtube_rss"
        assert (
            config.sources[0].uri
            == "https://www.youtube.com/feeds/videos.xml?channel_id=UCkyHDwRWMEluOEYmOGJ_2nw"
        )

    def test_huggingface_dataset_validates(self):
        config = SourcesConfig.model_validate(
            {
                "sources": [
                    {
                        "type": "huggingface_dataset",
                        "uri": "librarian-bots/arxiv-metadata-snapshot",
                        "max_samples": 100,
                        "fetch_content": True,
                    },
                ],
            }
        )

        entry = config.sources[0]
        assert isinstance(entry, HuggingFaceDatasetSource)
        assert entry.uri == "librarian-bots/arxiv-metadata-snapshot"
        assert entry.max_samples == 100
        assert entry.fetch_content is True
        # Defaults preserved for unspecified fields.
        assert entry.batch_size == 50
        assert entry.concurrency == 10


class TestVariantDefaults:
    def test_huggingface_dataset_defaults(self):
        config = SourcesConfig.model_validate(
            {
                "sources": [
                    {
                        "type": "huggingface_dataset",
                        "uri": "librarian-bots/arxiv-metadata-snapshot",
                    },
                ],
            }
        )

        entry = config.sources[0]
        assert isinstance(entry, HuggingFaceDatasetSource)
        assert entry.max_samples == 10
        assert entry.fetch_content is False
        assert entry.batch_size == 50
        assert entry.concurrency == 10

    def test_huggingface_dataset_uri_is_dataset_id(self):
        # Dataset id is NOT a URL; should still validate as long as it's non-empty.
        config = SourcesConfig.model_validate(
            {
                "sources": [
                    {
                        "type": "huggingface_dataset",
                        "uri": "librarian-bots/arxiv-metadata-snapshot",
                    },
                ],
            }
        )

        assert config.sources[0].uri == "librarian-bots/arxiv-metadata-snapshot"


class TestValidationErrors:
    def test_unknown_type_raises_validation_error(self):
        with pytest.raises(ValidationError) as exc_info:
            SourcesConfig.model_validate(
                {
                    "sources": [
                        {"type": "rss", "uri": "https://x.com/feed"},
                    ],
                }
            )

        assert "rss" in str(exc_info.value)

    @pytest.mark.parametrize(
        "source_type",
        [
            "substack_rss",
            "substack_article",
            "huggingface_dataset",
            "youtube_video",
            "youtube_rss",
            "web",
        ],
    )
    def test_missing_uri_raises_validation_error(self, source_type: str):
        with pytest.raises(ValidationError) as exc_info:
            SourcesConfig.model_validate({"sources": [{"type": source_type}]})

        assert "uri" in str(exc_info.value)


class TestUntypedEntryNormalization:
    def test_untyped_entry_with_substack_subdomain_normalizes_to_article(self):
        config = SourcesConfig.model_validate(
            {
                "sources": [
                    {"uri": "https://maximelabonne.substack.com/p/some-article"},
                ],
            }
        )

        entry = config.sources[0]
        assert isinstance(entry, SubstackArticleSource)
        assert entry.uri == "https://maximelabonne.substack.com/p/some-article"

    def test_untyped_entry_with_unknown_url_normalizes_to_web(self):
        config = SourcesConfig.model_validate(
            {
                "sources": [
                    {"uri": "https://news.ycombinator.com/item?id=123"},
                ],
            }
        )

        entry = config.sources[0]
        assert isinstance(entry, WebSource)
        assert entry.uri == "https://news.ycombinator.com/item?id=123"

    def test_untyped_entry_with_custom_substack_domain_normalizes_to_article(self):
        config = SourcesConfig.model_validate(
            {
                "sources": [
                    {"type": "substack_rss", "uri": "https://customblog.com/feed"},
                    {"uri": "https://customblog.com/p/some-article"},
                ],
            }
        )

        assert isinstance(config.sources[0], SubstackRssSource)
        second = config.sources[1]
        assert isinstance(second, SubstackArticleSource)
        assert second.uri == "https://customblog.com/p/some-article"

    @pytest.mark.parametrize(
        "uri",
        [
            "https://www.youtube.com/watch?v=eYaWxljC4sA",
            "https://youtube.com/watch?v=eYaWxljC4sA",
            "https://m.youtube.com/watch?v=eYaWxljC4sA",
            "https://youtu.be/eYaWxljC4sA",
            "https://www.youtube.com/shorts/abc123XYZ45",
        ],
    )
    def test_untyped_entry_with_youtube_video_url_normalizes_to_youtube_video(
        self, uri: str
    ):
        config = SourcesConfig.model_validate({"sources": [{"uri": uri}]})

        entry = config.sources[0]
        assert isinstance(entry, YouTubeVideoSource)
        assert entry.uri == uri

    def test_untyped_entry_with_youtube_rss_feed_normalizes_to_youtube_rss(self):
        uri = "https://www.youtube.com/feeds/videos.xml?channel_id=UCkyHDwRWMEluOEYmOGJ_2nw"
        config = SourcesConfig.model_validate({"sources": [{"uri": uri}]})

        entry = config.sources[0]
        assert isinstance(entry, YouTubeRssSource)
        assert entry.uri == uri

    def test_untyped_entry_on_bare_substack_com_normalizes_to_article(self):
        config = SourcesConfig.model_validate(
            {
                "sources": [
                    {"uri": "https://substack.com/something"},
                ],
            }
        )

        assert isinstance(config.sources[0], SubstackArticleSource)


class TestYamlRoundTrip:
    def test_yaml_round_trip_typed_and_untyped_mix(self, tmp_path):
        config_file = tmp_path / "sources.yaml"
        config_file.write_text(
            textwrap.dedent("""\
                sources:
                  sources:
                    - type: substack_rss
                      uri: https://www.decodingai.com/feed
                    - type: substack_article
                      uri: https://www.decodingai.com/p/some-article
                    - type: huggingface_dataset
                      uri: librarian-bots/arxiv-metadata-snapshot
                      max_samples: 25
                    - type: web
                      uri: https://www.anthropic.com/engineering/harness-design
                    - uri: https://www.reddit.com/r/AI_Agents
                    - uri: https://maximelabonne.substack.com/p/article
            """)
        )

        config = load_app_config(config_file)

        sources = config.sources.sources
        assert len(sources) == 6
        # No raw dicts — every entry is a typed Pydantic instance.
        assert all(
            isinstance(
                s,
                (
                    SubstackRssSource,
                    SubstackArticleSource,
                    HuggingFaceDatasetSource,
                    WebSource,
                    YouTubeVideoSource,
                    YouTubeRssSource,
                ),
            )
            for s in sources
        )

        assert isinstance(sources[0], SubstackRssSource)
        assert isinstance(sources[1], SubstackArticleSource)
        assert isinstance(sources[2], HuggingFaceDatasetSource)
        assert sources[2].max_samples == 25
        assert sources[2].batch_size == 50  # default preserved
        assert isinstance(sources[3], WebSource)
        # Untyped entries normalized.
        assert isinstance(sources[4], WebSource)
        assert isinstance(sources[5], SubstackArticleSource)


class TestSourcesConfigDefault:
    def test_default_sources_is_empty_list(self):
        config = SourcesConfig()

        assert config.sources == []
