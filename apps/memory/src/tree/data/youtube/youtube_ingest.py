from __future__ import annotations

import logging

from beanie import PydanticObjectId
from prefect import task

from tree.data.batch import gather_isolated
from tree.data.youtube.gemini_transcript_fetcher import GeminiTranscriptFetcher
from tree.data.youtube.types import FetchedTranscript, VideoMetadata
from tree.data.youtube.urls import extract_video_id
from tree.data.youtube.youtube_video import build_document, load_video_document
from tree.entities.documents import Document

logger = logging.getLogger(__name__)

# A resolved, transcribed slot ready to build: the canonical URL, its metadata, and
# the non-``None`` transcript the bulk fetch returned for it.
_TranscribedItem = tuple[str, VideoMetadata, FetchedTranscript]


@task(
    name="fetch-youtube-transcripts-batch",
    retries=2,
    retry_delay_seconds=5,
)
async def fetch_transcripts_batch(
    items: list[tuple[str, VideoMetadata]],
) -> list[_TranscribedItem]:
    """Fetch every video's transcript in ONE bulk ``fetch_many`` call.

    Constructs the ``GeminiTranscriptFetcher`` HERE (inside the task body) instead of
    receiving it as an argument, so the genai-client-holding, unpicklable fetcher is
    never a task input and Prefect's default cache policy can hash the inputs cleanly.
    Issues a SINGLE ``fetcher.fetch_many([url for url, _ in items])`` — the bulk
    transcript fetch shared by both pipelines (NO per-video re-fetch). Zips the
    transcripts back onto their ``(url, metadata)`` and returns only the slots whose
    transcript is non-``None``; a ``None`` slot is logged at WARNING (naming the video)
    and skipped — the user-facing signal the pipelines rely on. Network → ``retries=2``
    (the whole batch retries on a batch-WIDE failure; the bulk fetch already gives
    per-slot resilience, so we add no extra per-transcript retry).
    """

    if not items:
        return []

    fetcher = GeminiTranscriptFetcher()
    video_urls = [url for url, _ in items]
    transcripts = await fetcher.fetch_many(video_urls)

    resolved: list[_TranscribedItem] = []
    for (url, metadata), transcript in zip(items, transcripts, strict=True):
        if transcript is None:
            logger.warning("No transcript for %s; skipping", url)
            continue
        resolved.append((url, metadata, transcript))
    return resolved


@task(name="build-youtube-batch", retries=0)
async def build_batch(
    resolved: list[_TranscribedItem], user_id: PydanticObjectId
) -> list[Document]:
    """Pure map ``[(url, metadata, transcript)] -> [Document]`` over one batch.

    Runs the shared ``build_document`` per slot, resolving the bare 11-char
    ``video_id`` from the canonical URL. No network, no DB → ``retries=0``. Any slot
    whose URL cannot be re-resolved to an id is skipped defensively (it was already
    canonical, so this is unreachable in practice).
    """

    documents: list[Document] = []
    for url, metadata, transcript in resolved:
        video_id = extract_video_id(url)
        if video_id is None:  # pragma: no cover — defensive; url is canonical
            continue
        documents.append(
            build_document(
                video_id=video_id,
                metadata=metadata,
                transcript=transcript,
                user_id=user_id,
            )
        )
    return documents


@task(name="load-youtube-batch", retries=1, retry_delay_seconds=2)
async def load_batch(docs: list[Document]) -> list[Document]:
    """Dedup + persist one batch via a SINGLE isolated gather.

    Awaits the shared ``load_video_document`` per Document and returns the successful,
    non-``None`` subset (duplicates drop as ``None``); a per-element failure is logged
    at WARNING + skipped, NOT propagated — so one bad slot never sinks the batch.
    Retried whole-batch on a batch-WIDE infra failure (``retries=1``), safe via the
    ``(user_id, source_uri)`` dedup.
    """

    ingested, failures = await gather_isolated(docs, load_video_document)
    if failures:
        logger.warning("load_batch: %d/%d videos failed", failures, len(docs))
    return ingested


async def _bulk_build_and_load(
    items: list[tuple[str, VideoMetadata]],
    user_id: PydanticObjectId,
) -> list[Document]:
    """Shared core: "(url, metadata) list → ONE bulk fetch → build → load".

    The single tail both YouTube pipelines run, called ONCE per feed/batch:

    1. ``fetch_transcripts_batch`` — the SINGLE bulk ``fetch_many`` over all URLs
       (constructs the Gemini fetcher inside the task; drops ``None``-transcript slots
       with a per-slot WARNING).
    2. ``build_batch`` — ``build_document`` per resolved slot.
    3. ``load_batch`` — ``load_video_document`` per Document, isolated per element.

    Metadata is the CALLER's responsibility (oEmbed for direct video, feed for RSS):
    this core never fetches metadata itself, so neither metadata source regresses.
    """

    resolved = await fetch_transcripts_batch(items)
    documents = await build_batch(resolved, user_id)
    return await load_batch(documents)
