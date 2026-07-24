"""Unit tests for the pure helpers in `tree.data.youtube.types`.

`merge_video_metadata` is the ONE merge both transcript branches run (ADR-004,
Decision 5): the caller's BASE metadata (oEmbed for a single video, the Atom
feed entry for RSS) merged with whatever the fetched transcript carries, every
non-``None`` override field winning. Bright Data records fill most fields, the
Gemini branch only ``video_id`` — the same call covers both, so there is no
branch-specific merge logic to drift.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tree.data.youtube.types import VideoMetadata, merge_video_metadata

VIDEO_ID = "eYaWxljC4sA"
PUBLISH_DATE = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)


class TestMergeVideoMetadata:
    def test_non_none_override_fields_win(self) -> None:
        base = VideoMetadata(video_id=VIDEO_ID, title="oEmbed title", channel="oEmbed")
        override = VideoMetadata(
            video_id=VIDEO_ID,
            title="Bright Data title",
            channel="Bright Data channel",
        )

        merged = merge_video_metadata(base, override)

        assert merged.title == "Bright Data title"
        assert merged.channel == "Bright Data channel"

    def test_base_survives_where_override_is_none(self) -> None:
        base = VideoMetadata(video_id=VIDEO_ID, title="oEmbed title", channel="oEmbed")
        override = VideoMetadata(video_id=VIDEO_ID, description="A description")

        merged = merge_video_metadata(base, override)

        assert merged.title == "oEmbed title"
        assert merged.channel == "oEmbed"
        assert merged.description == "A description"

    def test_override_adds_fields_the_base_never_had(self) -> None:
        base = VideoMetadata(video_id=VIDEO_ID, title="oEmbed title")
        override = VideoMetadata(
            video_id=VIDEO_ID,
            publish_date=PUBLISH_DATE,
            duration_seconds=212,
            channel_id="UC38IQsAvIsxxjztdMZQtwHA",
        )

        merged = merge_video_metadata(base, override)

        assert merged.publish_date == PUBLISH_DATE
        assert merged.duration_seconds == 212
        assert merged.channel_id == "UC38IQsAvIsxxjztdMZQtwHA"

    def test_video_id_only_override_leaves_base_intact(self) -> None:
        # The Gemini branch: `transcript.metadata` carries ONLY `video_id`, so
        # the caller's base metadata must survive untouched.
        base = VideoMetadata(
            video_id=VIDEO_ID,
            title="Feed title",
            channel="Feed channel",
            publish_date=PUBLISH_DATE,
        )
        override = VideoMetadata(video_id=VIDEO_ID)

        merged = merge_video_metadata(base, override)

        assert merged == base

    def test_returns_a_new_instance_leaving_the_base_unmutated(self) -> None:
        base = VideoMetadata(video_id=VIDEO_ID, title="oEmbed title")
        override = VideoMetadata(video_id=VIDEO_ID, title="Bright Data title")

        merged = merge_video_metadata(base, override)

        assert merged is not base
        assert base.title == "oEmbed title"
