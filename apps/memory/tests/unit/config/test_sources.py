"""Unit tests for the shared source loader ``tree.config.sources``.

Covers the four public entry points the offline coordinator / online router /
CLI build on (ADR-003): ``load_sources`` (read + validate + concatenate, with
two-strategy path resolution), the cached ``default_configured_sources``,
``parse_uri_token`` (the ``URL`` / ``URL=TYPE`` CLI syntax), and
``build_uri_sources`` (type inference + ``huggingface_dataset`` rejection).
"""

from pathlib import Path

import pytest

from tree.config import sources as sources_module
from tree.config.sources import (
    SubstackArticleSource,
    SubstackRssSource,
    WebSource,
    YouTubeRssSource,
    YouTubeVideoSource,
)
from tree.config.sources import (
    BACKFILL_PATH,
    LISTEN_PATH,
    _resolve_source_path,
    build_uri_sources,
    default_configured_sources,
    load_sources,
    parse_uri_token,
)

# The real checkout root — what a managed run's cwd (its git clone) stands in for.
_REPO_ROOT = Path(__file__).resolve().parents[5]

# Committed-file shapes, mirrored from test_sources_files.py so the loader's
# read path is pinned to the same counts the source files themselves assert.
_BACKFILL_COUNT = 14
_LISTEN_COUNT = 3

_ALL_TYPE_LITERALS = (
    "substack_rss",
    "substack_article",
    "huggingface_dataset",
    "youtube_video",
    "youtube_rss",
    "web",
)


@pytest.fixture(autouse=True)
def _clear_default_cache():
    """Start and end every test with a cold ``default_configured_sources`` cache.

    The cache is process-global, so without this a warm entry from another test
    would mask the behavior under test (caching, recompute-after-clear).
    """

    default_configured_sources.cache_clear()
    yield
    default_configured_sources.cache_clear()


class TestPathConstants:
    def test_default_paths_stay_relative(self):
        """Relative, so :func:`_resolve_source_path`'s cwd fallback can run.

        An absolute path takes the is-file-or-raise branch and never reaches the
        cwd fallback — see the managed-run regression below.
        """

        assert not BACKFILL_PATH.is_absolute()
        assert not LISTEN_PATH.is_absolute()

    def test_both_committed_files_resolve(self):
        assert _resolve_source_path(BACKFILL_PATH).is_file()
        assert _resolve_source_path(LISTEN_PATH).is_file()

    def test_resolves_via_cwd_when_the_module_derived_repo_root_is_wrong(
        self, monkeypatch, tmp_path
    ):
        """Regression: pip-installed layout + git-clone cwd (Prefect Managed).

        ``_REPO_ROOT`` is derived by counting parents from the module file, which
        only holds in a source checkout. Installed into site-packages it points at
        the install prefix (``/usr/local``), which has no ``sources/`` — the cwd
        (the run's git clone) is what resolves. When the defaults were absolute
        this fallback was skipped and every managed HuggingFace worker died with
        ``FileNotFoundError: /usr/local/sources/backfill.yaml``.
        """

        # Arrange — bogus repo root, cwd on the real checkout.
        monkeypatch.setattr(sources_module, "_REPO_ROOT", tmp_path)
        monkeypatch.chdir(_REPO_ROOT)

        # Act / Assert
        assert _resolve_source_path(BACKFILL_PATH).is_file()


class TestLoadSources:
    def test_reads_backfill_file(self):
        entries = load_sources([BACKFILL_PATH])

        assert len(entries) == _BACKFILL_COUNT

    def test_reads_listen_file_as_substack_rss_feeds(self):
        entries = load_sources([LISTEN_PATH])

        assert len(entries) == _LISTEN_COUNT
        assert all(isinstance(entry, SubstackRssSource) for entry in entries)

    def test_concatenates_files_in_given_order(self):
        entries = load_sources([BACKFILL_PATH, LISTEN_PATH])

        assert len(entries) == _BACKFILL_COUNT + _LISTEN_COUNT
        # Backfill entries come first, listen entries last.
        assert all(
            not isinstance(entry, SubstackRssSource)
            for entry in entries[:_BACKFILL_COUNT]
        )
        assert all(
            isinstance(entry, SubstackRssSource) for entry in entries[_BACKFILL_COUNT:]
        )

    def test_order_follows_paths_argument(self):
        listen_first = load_sources([LISTEN_PATH, BACKFILL_PATH])

        assert all(
            isinstance(entry, SubstackRssSource)
            for entry in listen_first[:_LISTEN_COUNT]
        )

    def test_untyped_entries_infer_their_type(self):
        # backfill.yaml carries untyped web URLs that must infer to WebSource,
        # proving load_sources validates through SourcesConfig (not a raw parse).
        entries = load_sources([BACKFILL_PATH])

        assert any(isinstance(entry, WebSource) for entry in entries)


class TestLoadSourcesPathResolution:
    def test_relative_path_resolves_under_repo_root(self, monkeypatch, tmp_path):
        # cwd has no sources/ dir, so resolution must fall back to the
        # module-derived repo root — this is the local-serve cwd=apps/memory case.
        monkeypatch.chdir(tmp_path)

        entries = load_sources(["sources/backfill.yaml"])

        assert len(entries) == _BACKFILL_COUNT

    def test_relative_path_resolves_under_cwd(self, monkeypatch, tmp_path):
        feed_dir = tmp_path / "sub"
        feed_dir.mkdir()
        (feed_dir / "feed.yaml").write_text(
            "- uri: https://example.substack.com/feed\n  type: substack_rss\n"
        )
        monkeypatch.chdir(tmp_path)

        # repo_root/sub/feed.yaml does not exist, so this only resolves via cwd.
        entries = load_sources(["sub/feed.yaml"])

        assert len(entries) == 1
        assert entries[0].uri == "https://example.substack.com/feed"

    def test_missing_relative_path_raises_naming_both_locations(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.chdir(tmp_path)

        with pytest.raises(FileNotFoundError) as exc_info:
            load_sources(["does/not/exist.yaml"])

        message = str(exc_info.value)
        assert str(sources_module._REPO_ROOT) in message
        assert str(tmp_path) in message

    def test_missing_absolute_path_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_sources([tmp_path / "nope.yaml"])


class TestDefaultConfiguredSources:
    def test_equals_backfill_plus_listen_union(self):
        union = load_sources([BACKFILL_PATH, LISTEN_PATH])

        assert default_configured_sources() == union

    def test_returns_cached_object_on_repeat_calls(self):
        first = default_configured_sources()
        second = default_configured_sources()

        assert first is second

    def test_caches_load_sources_result(self, mocker):
        sentinel = [SubstackRssSource(uri="https://x.substack.com/feed")]
        mock = mocker.patch.object(
            sources_module, "load_sources", return_value=sentinel
        )

        default_configured_sources()
        default_configured_sources()

        mock.assert_called_once_with([BACKFILL_PATH, LISTEN_PATH])

    def test_cache_clear_forces_recompute(self, mocker):
        mock = mocker.patch.object(sources_module, "load_sources", return_value=[])

        default_configured_sources()
        default_configured_sources()
        assert mock.call_count == 1

        default_configured_sources.cache_clear()
        default_configured_sources()
        assert mock.call_count == 2


class TestParseUriToken:
    @pytest.mark.parametrize(
        "token",
        [
            "https://example.com",
            "https://www.decodingai.com/feed",
            "librarian-bots/arxiv-metadata-snapshot",
        ],
    )
    def test_bare_token_has_no_type(self, token: str):
        assert parse_uri_token(token) == (token, None)

    def test_query_string_url_with_equals_stays_intact(self):
        token = (
            "https://www.youtube.com/feeds/videos.xml"
            "?channel_id=UCsBjURrPoezykLs9EqgamOA"
        )

        assert parse_uri_token(token) == (token, None)

    @pytest.mark.parametrize("source_type", _ALL_TYPE_LITERALS)
    def test_splits_on_trailing_valid_type_literal(self, source_type: str):
        token = f"https://example.com/thing={source_type}"

        assert parse_uri_token(token) == ("https://example.com/thing", source_type)

    def test_splits_substack_rss_example(self):
        token = "https://www.decodingai.com/feed=substack_rss"

        assert parse_uri_token(token) == (
            "https://www.decodingai.com/feed",
            "substack_rss",
        )

    def test_trailing_equals_non_type_is_not_split(self):
        token = "https://example.com/page=notarealtype"

        assert parse_uri_token(token) == (token, None)


class TestBuildUriSources:
    def test_empty_specs_returns_empty_list(self):
        assert build_uri_sources([]) == []

    def test_typed_spec_is_honored_over_inference(self):
        # Inference would call this a WebSource; the explicit type must win.
        sources = build_uri_sources(
            [("https://news.ycombinator.com/item?id=1", "substack_article")]
        )

        assert isinstance(sources[0], SubstackArticleSource)

    def test_untyped_specs_are_inferred(self):
        sources = build_uri_sources(
            [
                ("https://news.ycombinator.com/item?id=1", None),
                ("https://maximelabonne.substack.com/p/article", None),
                ("https://www.youtube.com/watch?v=Sntj4HmuykI", None),
                (
                    "https://www.youtube.com/feeds/videos.xml?channel_id=UCabc",
                    None,
                ),
            ]
        )

        assert isinstance(sources[0], WebSource)
        assert isinstance(sources[1], SubstackArticleSource)
        assert isinstance(sources[2], YouTubeVideoSource)
        assert isinstance(sources[3], YouTubeRssSource)

    def test_resolves_mixed_typed_and_untyped_in_one_call(self):
        sources = build_uri_sources(
            [
                ("https://news.ycombinator.com/item?id=1", "substack_article"),
                ("https://maximelabonne.substack.com/p/article", None),
                ("https://www.youtube.com/watch?v=Sntj4HmuykI", None),
            ]
        )

        assert isinstance(sources[0], SubstackArticleSource)  # typed honored
        assert isinstance(sources[1], SubstackArticleSource)  # inferred
        assert isinstance(sources[2], YouTubeVideoSource)  # inferred

    def test_cross_entry_substack_host_inference(self):
        # An untyped URL on a custom domain inherits substack_article from an
        # earlier typed substack source on the same host — proving the specs
        # are normalized together in one SourcesConfig pass.
        sources = build_uri_sources(
            [
                ("https://customblog.com/feed", "substack_rss"),
                ("https://customblog.com/p/some-article", None),
            ]
        )

        assert isinstance(sources[0], SubstackRssSource)
        assert isinstance(sources[1], SubstackArticleSource)

    def test_rejects_explicit_huggingface_dataset_spec(self):
        with pytest.raises(ValueError, match="sources/backfill.yaml"):
            build_uri_sources(
                [("librarian-bots/arxiv-metadata-snapshot", "huggingface_dataset")]
            )

    def test_rejects_huggingface_even_when_mixed_with_valid_specs(self):
        with pytest.raises(ValueError, match="huggingface_dataset"):
            build_uri_sources(
                [
                    ("https://news.ycombinator.com/item?id=1", None),
                    ("librarian-bots/arxiv-metadata-snapshot", "huggingface_dataset"),
                ]
            )

    def test_parse_then_build_round_trip(self):
        # The realistic CLI flow: parse raw --uri tokens, then build.
        tokens = [
            "https://news.ycombinator.com/item?id=1",
            "https://www.decodingai.com/feed=substack_rss",
        ]
        specs = [parse_uri_token(token) for token in tokens]

        sources = build_uri_sources(specs)

        assert isinstance(sources[0], WebSource)
        assert isinstance(sources[1], SubstackRssSource)
        assert sources[1].uri == "https://www.decodingai.com/feed"
