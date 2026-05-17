import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.data.huggingface.arxiv_dataset_pipeline import ingest_arxiv_dataset
from tree.entities.documents import Document, SourceType

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")

FAKE_ENTRIES = [
    {
        "id": f"2103.{i:05d}",
        "submitter": f"Author {i}",
        "authors": f"Author {i}, Coauthor {i}",
        "title": f"Test Paper {i}",
        "comments": "10 pages",
        "journal-ref": None,
        "doi": None,
        "report-no": None,
        "categories": "cs.LG",
        "license": None,
        "abstract": f"Abstract for paper {i}.",
        "update_date": "2024-01-15",
    }
    for i in range(5)
]


class TestIngestArxivDatasetFlow:
    async def test_ingests_documents_via_prefect_flow(
        self, mongo_client, mocker
    ) -> None:
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.load_dataset",
            return_value=iter(FAKE_ENTRIES),
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.init_mongodb",
            return_value=mongo_client,
        )

        with prefect_tags("tests"):
            result = await ingest_arxiv_dataset(
                user_id=_USER_ID, max_samples=5, fetch_content=False
            )

        assert len(result) == 5
        for doc in result:
            assert doc.source_type == SourceType.HUGGINGFACE
            assert doc.id is not None
            assert doc.source_uri.startswith("https://arxiv.org/abs/")
            assert doc.summary != ""
            assert doc.content == ""

        db_docs = await Document.find(
            Document.source_type == SourceType.HUGGINGFACE
        ).to_list()
        assert len(db_docs) == 5

    @pytest.mark.slow
    async def test_idempotent_on_rerun(self, mongo_client, mocker) -> None:
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.load_dataset",
            return_value=iter(FAKE_ENTRIES),
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.init_mongodb",
            return_value=mongo_client,
        )

        with prefect_tags("tests"):
            first_run = await ingest_arxiv_dataset(
                user_id=_USER_ID, max_samples=5, fetch_content=False
            )
        assert len(first_run) == 5

        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.load_dataset",
            return_value=iter(FAKE_ENTRIES),
        )

        with prefect_tags("tests"):
            second_run = await ingest_arxiv_dataset(
                user_id=_USER_ID, max_samples=5, fetch_content=False
            )
        assert len(second_run) == 0

        db_docs = await Document.find(
            Document.source_type == SourceType.HUGGINGFACE
        ).to_list()
        assert len(db_docs) == 5

    @pytest.mark.slow
    async def test_with_fetch_content(self, mongo_client, mocker) -> None:
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.load_dataset",
            return_value=iter(FAKE_ENTRIES[:2]),
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.httpx.AsyncClient",
            return_value=_make_mock_client(mocker, "Full paper text."),
        )

        with prefect_tags("tests"):
            result = await ingest_arxiv_dataset(
                user_id=_USER_ID, max_samples=2, fetch_content=True
            )

        assert len(result) == 2
        for doc in result:
            assert doc.content == "Full paper text."

    async def test_batch_processing(self, mongo_client, mocker) -> None:
        entries = [
            {
                "id": f"2401.{i:05d}",
                "authors": f"Author {i}",
                "title": f"Batch Paper {i}",
                "abstract": f"Abstract {i}.",
                "update_date": "2024-06-01",
            }
            for i in range(7)
        ]
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.load_dataset",
            return_value=iter(entries),
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.init_mongodb",
            return_value=mongo_client,
        )

        with prefect_tags("tests"):
            result = await ingest_arxiv_dataset(
                user_id=_USER_ID, max_samples=7, fetch_content=False
            )

        assert len(result) == 7
        db_docs = await Document.find(
            Document.source_type == SourceType.HUGGINGFACE
        ).to_list()
        assert len(db_docs) == 7

    async def test_upgrades_latent_document(self, mongo_client, mocker) -> None:
        latent = Document(
            source_type=SourceType.LATENT,
            source_uri="https://arxiv.org/abs/2103.00000",
            user_id=_USER_ID,
        )
        await latent.insert()

        mocker.patch(
            "tree.data.huggingface.arxiv_dataset.load_dataset",
            return_value=iter(FAKE_ENTRIES[:1]),
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.init_mongodb",
            return_value=mongo_client,
        )

        with prefect_tags("tests"):
            result = await ingest_arxiv_dataset(
                user_id=_USER_ID, max_samples=1, fetch_content=False
            )

        assert len(result) == 1
        assert result[0].id == latent.id
        assert result[0].source_type == SourceType.HUGGINGFACE
        assert result[0].title == "Test Paper 0"


def _make_mock_client(mocker, text: str):
    html = f"<html><body><article>{text}</article></body></html>"
    mock_response = mocker.Mock()
    mock_response.status_code = 200
    mock_response.text = html

    mock_client = mocker.AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
    return mock_client
