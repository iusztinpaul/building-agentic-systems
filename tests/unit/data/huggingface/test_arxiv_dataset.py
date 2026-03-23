from twin.data.huggingface.arxiv_dataset import (
    extract_document,
    fetch_dataset,
    parse_authors,
    parse_update_date,
)
from twin.entities.documents import SourceType

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
        doc = extract_document(SAMPLE_ENTRY)

        assert doc.source_type == SourceType.HUGGINGFACE
        assert doc.source_uri == "https://arxiv.org/abs/2103.12345"
        assert doc.title == "A Study on Neural Networks"
        assert doc.summary == "cs.LG cs.AI"
        assert doc.content == "We present a novel approach to neural networks."
        assert doc.authors == ["Jane Doe", "John Smith", "Alice Johnson"]
        assert doc.date.year == 2021
        assert doc.date.month == 3
        assert doc.references == []

    def test_empty_entry(self) -> None:
        doc = extract_document({})

        assert doc.source_uri == ""
        assert doc.title == ""
        assert doc.content == ""
        assert doc.summary == ""
        assert doc.authors == ["Unknown"]

    def test_none_fields(self) -> None:
        entry = {
            "id": "2103.00001",
            "authors": None,
            "title": None,
            "abstract": None,
            "categories": None,
            "update_date": None,
        }
        doc = extract_document(entry)

        assert doc.source_uri == "https://arxiv.org/abs/2103.00001"
        assert doc.title == ""
        assert doc.content == ""
        assert doc.summary == ""
        assert doc.authors == ["Unknown"]
        assert doc.date.tzinfo is not None


class TestFetchDataset:
    def test_returns_limited_entries(self, mocker) -> None:
        fake_entries = [{"id": f"2103.{i:05d}"} for i in range(20)]
        mock_ds = iter(fake_entries)
        mocker.patch(
            "twin.data.huggingface.arxiv_dataset.load_dataset",
            return_value=mock_ds,
        )

        result = fetch_dataset(max_samples=5)

        assert len(result) == 5
        assert result[0]["id"] == "2103.00000"
        assert result[4]["id"] == "2103.00004"

    def test_returns_all_when_fewer_than_max(self, mocker) -> None:
        fake_entries = [{"id": f"2103.{i:05d}"} for i in range(3)]
        mock_ds = iter(fake_entries)
        mocker.patch(
            "twin.data.huggingface.arxiv_dataset.load_dataset",
            return_value=mock_ds,
        )

        result = fetch_dataset(max_samples=10)

        assert len(result) == 3

    def test_returns_empty_for_zero_max(self, mocker) -> None:
        fake_entries = [{"id": "2103.00000"}]
        mock_ds = iter(fake_entries)
        mocker.patch(
            "twin.data.huggingface.arxiv_dataset.load_dataset",
            return_value=mock_ds,
        )

        result = fetch_dataset(max_samples=0)

        assert result == []
