import httpx
import pytest
from beanie import PydanticObjectId

from tree.data.huggingface.arxiv_dataset import (
    extract_document,
    fetch_dataset_batches,
    fetch_paper_content,
    parse_authors,
    parse_update_date,
)
from tree.entities.documents import SourceType

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")

SAMPLE_ENTRY = {
    "id": "2103.12345",
    "submitter": "Jane Doe",
    "authors": "Jane Doe, John Smith, Alice Johnson",
    "title": "  A Study on Neural Networks  ",
    "comments": "10 pages, 5 figures",
    "journal-ref": "Nature 2021",
    "doi": "10.1234/example",
    "report-no": None,
    "categories": "cs.LG cs.AI",
    "license": "http://creativecommons.org/licenses/by/4.0/",
    "abstract": "  We present a novel approach to neural networks.  ",
    "update_date": "2021-03-24",
}


class TestParseUpdateDate:
    def test_valid_date(self) -> None:
        dt = parse_update_date("2021-03-24")
        assert dt.year == 2021
        assert dt.month == 3
        assert dt.day == 24
        assert dt.tzinfo is not None

    def test_date_with_whitespace(self) -> None:
        dt = parse_update_date("  2021-03-24  ")
        assert dt.year == 2021

    def test_none_returns_now(self) -> None:
        dt = parse_update_date(None)
        assert dt.tzinfo is not None

    def test_empty_string_returns_now(self) -> None:
        dt = parse_update_date("")
        assert dt.tzinfo is not None

    def test_invalid_format_returns_now(self) -> None:
        dt = parse_update_date("not-a-date")
        assert dt.tzinfo is not None


class TestParseAuthors:
    def test_multiple_authors(self) -> None:
        authors = parse_authors("Jane Doe, John Smith, Alice Johnson")
        assert authors == ["Jane Doe", "John Smith", "Alice Johnson"]

    def test_single_author(self) -> None:
        authors = parse_authors("Jane Doe")
        assert authors == ["Jane Doe"]

    def test_none_returns_unknown(self) -> None:
        assert parse_authors(None) == ["Unknown"]

    def test_empty_string_returns_unknown(self) -> None:
        assert parse_authors("") == ["Unknown"]

    def test_strips_whitespace(self) -> None:
        authors = parse_authors("  Jane Doe ,  John Smith  ")
        assert authors == ["Jane Doe", "John Smith"]


class TestExtractDocument:
    def test_maps_fields(self) -> None:
        doc = extract_document(SAMPLE_ENTRY, _USER_ID)

        assert doc.source_type == SourceType.HUGGINGFACE
        assert doc.source_uri == "https://arxiv.org/abs/2103.12345"
        assert doc.title == "A Study on Neural Networks"
        assert doc.summary == "We present a novel approach to neural networks."
        assert doc.content == ""
        assert doc.authors == ["Jane Doe", "John Smith", "Alice Johnson"]
        assert doc.date.year == 2021
        assert doc.date.month == 3
        assert doc.references == []

    def test_empty_entry_returns_none(self) -> None:
        result = extract_document({}, _USER_ID)

        assert result is None

    def test_none_fields(self) -> None:
        entry = {
            "id": "2103.00001",
            "authors": None,
            "title": None,
            "abstract": None,
            "categories": None,
            "update_date": None,
        }
        doc = extract_document(entry, _USER_ID)

        assert doc.source_uri == "https://arxiv.org/abs/2103.00001"
        assert doc.title == ""
        assert doc.content == ""
        assert doc.summary == ""
        assert doc.authors == ["Unknown"]
        assert doc.date.tzinfo is not None


class TestFetchPaperContent:
    @pytest.mark.asyncio
    async def test_successful_fetch(self, mocker) -> None:
        html = "<html><body><article>Full paper text here.</article></body></html>"
        mock_response = mocker.Mock()
        mock_response.status_code = 200
        mock_response.text = html

        mock_client = mocker.AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.httpx.AsyncClient",
            return_value=mock_client,
        )

        result = await fetch_paper_content("https://arxiv.org/abs/2401.00001")

        assert result == "Full paper text here."

    @pytest.mark.asyncio
    async def test_404_returns_empty(self, mocker) -> None:
        mock_response = mocker.Mock()
        mock_response.status_code = 404

        mock_client = mocker.AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.httpx.AsyncClient",
            return_value=mock_client,
        )

        result = await fetch_paper_content("https://arxiv.org/abs/0704.0001")

        assert result == ""

    @pytest.mark.asyncio
    async def test_no_article_element_returns_empty(self, mocker) -> None:
        html = "<html><body><p>No article tag</p></body></html>"
        mock_response = mocker.Mock()
        mock_response.status_code = 200
        mock_response.text = html

        mock_client = mocker.AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.httpx.AsyncClient",
            return_value=mock_client,
        )

        result = await fetch_paper_content("https://arxiv.org/abs/2401.00001")

        assert result == ""

    @pytest.mark.asyncio
    async def test_http_error_returns_empty(self, mocker) -> None:
        mock_client = mocker.AsyncMock()
        mock_client.get.side_effect = httpx.HTTPError("connection failed")
        mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.httpx.AsyncClient",
            return_value=mock_client,
        )

        result = await fetch_paper_content("https://arxiv.org/abs/2401.00001")

        assert result == ""

    @pytest.mark.asyncio
    async def test_empty_source_uri_returns_empty(self) -> None:
        assert await fetch_paper_content("") == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tag",
        ["article", "main"],
    )
    async def test_fallback_tags(self, mocker, tag: str) -> None:
        html = f"<html><body><{tag}>Content in {tag}.</{tag}></body></html>"
        mock_response = mocker.Mock()
        mock_response.status_code = 200
        mock_response.text = html

        mock_client = mocker.AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.httpx.AsyncClient",
            return_value=mock_client,
        )

        result = await fetch_paper_content("https://arxiv.org/abs/2401.00001")

        assert result == f"Content in {tag}."


class TestFetchDatasetBatches:
    def test_yields_correct_batch_sizes(self, mocker) -> None:
        fake_entries = [{"id": f"2103.{i:05d}"} for i in range(10)]
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.load_dataset",
            return_value=iter(fake_entries),
        )

        batches = list(fetch_dataset_batches(max_samples=10, batch_size=3))

        assert len(batches) == 4
        assert len(batches[0]) == 3
        assert len(batches[1]) == 3
        assert len(batches[2]) == 3
        assert len(batches[3]) == 1

    def test_stops_at_max_samples(self, mocker) -> None:
        fake_entries = [{"id": f"2103.{i:05d}"} for i in range(20)]
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.load_dataset",
            return_value=iter(fake_entries),
        )

        batches = list(fetch_dataset_batches(max_samples=5, batch_size=3))
        total = sum(len(b) for b in batches)

        assert total == 5
        assert len(batches) == 2
        assert len(batches[0]) == 3
        assert len(batches[1]) == 2

    def test_returns_all_when_fewer_than_max(self, mocker) -> None:
        fake_entries = [{"id": f"2103.{i:05d}"} for i in range(3)]
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.load_dataset",
            return_value=iter(fake_entries),
        )

        batches = list(fetch_dataset_batches(max_samples=10, batch_size=5))

        assert len(batches) == 1
        assert len(batches[0]) == 3

    def test_returns_empty_for_zero_max(self, mocker) -> None:
        fake_entries = [{"id": "2103.00000"}]
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.load_dataset",
            return_value=iter(fake_entries),
        )

        batches = list(fetch_dataset_batches(max_samples=0, batch_size=5))

        assert batches == []

    def test_exact_batch_boundary(self, mocker) -> None:
        fake_entries = [{"id": f"2103.{i:05d}"} for i in range(6)]
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.load_dataset",
            return_value=iter(fake_entries),
        )

        batches = list(fetch_dataset_batches(max_samples=6, batch_size=3))

        assert len(batches) == 2
        assert all(len(b) == 3 for b in batches)


class _FakeStreamingDataset:
    """Minimal stand-in for a streaming ``IterableDataset``.

    Holds an indexed list of rows and exposes ``.skip(n)`` (mirroring
    ``IterableDataset.skip`` — discards the first ``n`` rows and returns a new
    dataset) so the offset/window math can be exercised without the live HF API.
    Records every ``.skip`` call on the ROOT instance so tests can assert it was
    (or was not) applied.
    """

    def __init__(
        self, rows: list[dict], *, _root: "_FakeStreamingDataset | None" = None
    ) -> None:
        self._rows = rows
        self._root = _root or self
        if _root is None:
            self.skip_calls: list[int] = []

    def skip(self, n: int) -> "_FakeStreamingDataset":
        self._root.skip_calls.append(n)
        return _FakeStreamingDataset(self._rows[n:], _root=self._root)

    def __iter__(self):
        return iter(self._rows)


class TestFetchDatasetBatchesOffset:
    """The #071 offset window: ``[offset, offset + max_samples)``."""

    def test_offset_none_never_skips(self, mocker) -> None:
        fake = _FakeStreamingDataset([{"id": f"2103.{i:05d}"} for i in range(20)])
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.load_dataset",
            return_value=fake,
        )

        batches = list(fetch_dataset_batches(max_samples=10, batch_size=5, offset=None))
        rows = [r for b in batches for r in b]

        assert fake.skip_calls == []
        assert len(batches) == 2
        assert [r["id"] for r in rows] == [f"2103.{i:05d}" for i in range(10)]

    def test_offset_zero_never_skips(self, mocker) -> None:
        fake = _FakeStreamingDataset([{"id": f"2103.{i:05d}"} for i in range(20)])
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.load_dataset",
            return_value=fake,
        )

        batches = list(fetch_dataset_batches(max_samples=10, batch_size=5, offset=0))
        rows = [r for b in batches for r in b]

        assert fake.skip_calls == []
        assert [r["id"] for r in rows] == [f"2103.{i:05d}" for i in range(10)]

    def test_offset_default_matches_pre_feature_stream(self, mocker) -> None:
        # With no ``offset`` argument at all, the result is byte-for-byte the
        # pre-feature single-run ingest (the first ``max_samples`` rows, no skip).
        fake_with = _FakeStreamingDataset([{"id": f"2103.{i:05d}"} for i in range(20)])
        fake_without = _FakeStreamingDataset(
            [{"id": f"2103.{i:05d}"} for i in range(20)]
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.load_dataset",
            side_effect=[fake_without, fake_with],
        )

        without_offset = list(fetch_dataset_batches(max_samples=10, batch_size=5))
        with_offset_zero = list(
            fetch_dataset_batches(max_samples=10, batch_size=5, offset=0)
        )

        assert without_offset == with_offset_zero
        assert fake_without.skip_calls == []
        assert fake_with.skip_calls == []

    def test_offset_skips_then_windows(self, mocker) -> None:
        fake = _FakeStreamingDataset([{"id": f"2103.{i:05d}"} for i in range(30)])
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.load_dataset",
            return_value=fake,
        )

        batches = list(fetch_dataset_batches(max_samples=10, batch_size=5, offset=5))
        rows = [r for b in batches for r in b]

        # ``.skip(5)`` applied once, before iteration; window is [5, 15).
        assert fake.skip_calls == [5]
        assert len(rows) == 10
        assert [r["id"] for r in rows] == [f"2103.{i:05d}" for i in range(5, 15)]

    def test_window_log_line_includes_offset(self, mocker, caplog) -> None:
        fake = _FakeStreamingDataset([{"id": f"2103.{i:05d}"} for i in range(30)])
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.load_dataset",
            return_value=fake,
        )

        with caplog.at_level("INFO", logger="tree.data.huggingface.arxiv_dataset"):
            list(fetch_dataset_batches(max_samples=10, batch_size=5, offset=5))

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "offset=5" in messages

    def test_negative_offset_raises(self, mocker) -> None:
        fake = _FakeStreamingDataset([{"id": f"2103.{i:05d}"} for i in range(30)])
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.load_dataset",
            return_value=fake,
        )

        with pytest.raises(ValueError, match="offset"):
            list(fetch_dataset_batches(max_samples=10, batch_size=5, offset=-1))

        # ``.skip`` must NEVER be called with a negative value.
        assert fake.skip_calls == []
