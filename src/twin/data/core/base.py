import abc

from twin.entities.documents import Document


class BaseETL(abc.ABC):
    @abc.abstractmethod
    async def extract_one(self, raw_entry: dict) -> Document:
        """Transform a single raw entry into a Document (without persisting)."""

    @abc.abstractmethod
    async def run(self, source_uri: str) -> list[Document]:
        """Fetch, extract, and persist all entries from a single source."""

    @abc.abstractmethod
    async def run_batch(self, source_uris: list[str]) -> list[Document]:
        """Process multiple sources sequentially."""
