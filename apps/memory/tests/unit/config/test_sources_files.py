"""Unit tests for the committed source files under repo-root ``sources/``.

These guard that ``sources/listen.yaml`` and ``sources/backfill.yaml`` stay
parseable by ``SourcesConfig`` and keep their cadence split: ``listen.yaml``
holds only polled RSS feeds, ``backfill.yaml`` holds one-shot ingests. The
files carry NO ``scheduled`` key (retired in ``sources-config-split``).
"""

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from tree.config.sources import (
    HuggingFaceDatasetSource,
    SourcesConfig,
    SubstackArticleSource,
    SubstackRssSource,
    WebSource,
    YouTubeVideoSource,
)

# The test lives at apps/memory/tests/unit/config/; the repo root (where the
# committed ``sources/`` dir lives) is five parents up
# (config -> unit -> tests -> memory -> apps -> repo root).
_SOURCES_DIR = Path(__file__).resolve().parents[5] / "sources"
_LISTEN_FILE = _SOURCES_DIR / "listen.yaml"
_BACKFILL_FILE = _SOURCES_DIR / "backfill.yaml"


def _load_raw(path: Path) -> Any:
    """Parse a source file as raw YAML (the shape ``SourcesConfig`` accepts)."""

    return yaml.safe_load(path.read_text())


def _type_counts(config: SourcesConfig) -> Counter[str]:
    """Count parsed source entries by their discriminator ``type``."""

    return Counter(entry.type for entry in config.sources)


@pytest.fixture
def listen_config() -> SourcesConfig:
    return SourcesConfig.model_validate(_load_raw(_LISTEN_FILE))


@pytest.fixture
def backfill_config() -> SourcesConfig:
    return SourcesConfig.model_validate(_load_raw(_BACKFILL_FILE))


class TestFilesExistAsTopLevelLists:
    """Each source file is a top-level YAML list (the flat shape the loader accepts)."""

    @pytest.mark.parametrize("path", [_LISTEN_FILE, _BACKFILL_FILE])
    def test_source_file_exists(self, path: Path):
        assert path.is_file()

    @pytest.mark.parametrize("path", [_LISTEN_FILE, _BACKFILL_FILE])
    def test_source_file_is_top_level_list(self, path: Path):
        assert isinstance(_load_raw(path), list)


class TestListenYaml:
    """``listen.yaml`` holds ONLY the polled RSS feeds."""

    def test_parses_through_sources_config(self, listen_config: SourcesConfig):
        # Three active substack_rss feeds; the youtube_rss example stays commented.
        assert len(listen_config.sources) == 3

    def test_contains_only_substack_rss_feeds(self, listen_config: SourcesConfig):
        assert _type_counts(listen_config) == Counter({"substack_rss": 3})

    def test_every_entry_is_a_substack_rss_source(self, listen_config: SourcesConfig):
        assert all(
            isinstance(entry, SubstackRssSource) for entry in listen_config.sources
        )

    def test_carries_the_expected_feed_uris(self, listen_config: SourcesConfig):
        assert {entry.uri for entry in listen_config.sources} == {
            "https://www.decodingai.com/feed",
            "https://maximelabonne.substack.com/feed",
            "https://www.latent.space/feed",
        }


class TestBackfillYaml:
    """``backfill.yaml`` holds everything that is ingested once."""

    def test_parses_through_sources_config(self, backfill_config: SourcesConfig):
        assert len(backfill_config.sources) == 14

    def test_variant_counts_match_the_cadence_split(
        self, backfill_config: SourcesConfig
    ):
        assert _type_counts(backfill_config) == Counter(
            {
                "substack_article": 10,
                "web": 2,
                "huggingface_dataset": 1,
                "youtube_video": 1,
            }
        )

    def test_untyped_web_urls_infer_to_web(self, backfill_config: SourcesConfig):
        web_uris = {
            entry.uri
            for entry in backfill_config.sources
            if isinstance(entry, WebSource)
        }
        assert web_uris == {
            "https://www.reddit.com/r/LLMDevs/comments/1ts3qc3/a_year_building_agent_memory_on_knowledge_graphs/",
            "https://www.anthropic.com/engineering/harness-design-long-running-apps",
        }

    def test_youtube_video_is_typed(self, backfill_config: SourcesConfig):
        videos = [
            entry
            for entry in backfill_config.sources
            if isinstance(entry, YouTubeVideoSource)
        ]
        assert len(videos) == 1
        assert videos[0].uri == "https://www.youtube.com/watch?v=Sntj4HmuykI"

    def test_substack_articles_are_typed(self, backfill_config: SourcesConfig):
        articles = [
            entry
            for entry in backfill_config.sources
            if isinstance(entry, SubstackArticleSource)
        ]
        assert len(articles) == 10
        assert all(
            entry.uri.startswith("https://www.decodingai.com/p/") for entry in articles
        )

    def test_huggingface_dataset_tuning_preserved_verbatim(
        self, backfill_config: SourcesConfig
    ):
        datasets = [
            entry
            for entry in backfill_config.sources
            if isinstance(entry, HuggingFaceDatasetSource)
        ]
        assert len(datasets) == 1
        entry = datasets[0]
        assert entry.uri == "librarian-bots/arxiv-metadata-snapshot"
        assert entry.max_samples == 1000
        assert entry.fetch_content is False
        assert entry.num_workers == 2
        assert entry.batch_size == 50
        assert entry.concurrency == 25


class TestScheduledKeyRetired:
    """No source file carries a ``scheduled`` key — cadence is the filename now."""

    @pytest.mark.parametrize("path", [_LISTEN_FILE, _BACKFILL_FILE])
    def test_no_entry_has_a_scheduled_key(self, path: Path):
        entries = _load_raw(path)
        assert all("scheduled" not in entry for entry in entries)
