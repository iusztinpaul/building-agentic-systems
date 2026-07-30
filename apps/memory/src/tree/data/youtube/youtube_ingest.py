"""Shared bulk core of BOTH YouTube pipelines — the transcript fallback chain.

"(url, metadata) list → Bright Data bulk fetch → Gemini fallback over ONLY the
transcript-less slots → build → load", run identically by the single-video MCP flow
and the RSS/offline batch flow (ADR-004, Decision 1: no per-path branching).

Both fetchers are constructed INSIDE the task body, never passed in, so a
client-holding unpicklable object is never a Prefect task input. Credential
PRESENCE is the only backend switch: an unconfigured backend is never constructed
(both constructors raise on a missing key), and with NEITHER configured the task
raises up-front, before any billable call.
"""

from __future__ import annotations

import logging

from beanie import PydanticObjectId
from prefect import task
from pydantic import SecretStr

from tree.config.settings import settings
from tree.data.batch import gather_isolated
from tree.data.web.web_scraper_api import BrightDataTimeoutError
from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
)
from tree.data.youtube.brightdata_transcript_fetcher import BrightDataTranscriptFetcher
from tree.data.youtube.gemini_transcript_fetcher import GeminiTranscriptFetcher
from tree.data.youtube.types import (
    FetchedTranscript,
    VideoMetadata,
    merge_video_metadata,
)
from tree.data.youtube.youtube import (
    INVALID_URL_ERROR,
    build_document,
    build_failure_document,
    extract_video_id,
    load_video_document,
)
from tree.entities.documents import Document

logger = logging.getLogger(__name__)

# A resolved, transcribed slot ready to build: the canonical URL, its metadata, and
# the non-``None`` transcript the fallback chain produced for it.
_TranscribedItem = tuple[str, VideoMetadata, FetchedTranscript]

# A slot the chain could NOT transcribe: the key to persist it under (canonical URL,
# or the RAW input when nothing resolved), whatever base metadata exists (``None``
# for an unresolvable input), and the normalized ``ingest_error``.
_FailedItem = tuple[str, VideoMetadata | None, str]

# Why a slot fell back — logged verbatim in every cost WARNING so an operator can
# tell "this one video has no captions" from "the whole collection failed".
_REASON_NO_BRIGHTDATA_TRANSCRIPT = "no_brightdata_transcript"
_REASON_BRIGHTDATA_NOT_CONFIGURED = "brightdata_not_configured"
_REASON_BRIGHTDATA_REQUEST_ERROR = "brightdata_request_error"
_REASON_BRIGHTDATA_TIMEOUT = "brightdata_timeout"

# Backend states as they read inside a normalized ``no_transcript: …`` message.
_STATE_EMPTY = "returned empty"
_STATE_NOT_CONFIGURED = "not configured"
_STATE_FETCH_FAILED = "unavailable (fetch failed)"

_BRIGHTDATA_STATES = {
    _REASON_NO_BRIGHTDATA_TRANSCRIPT: _STATE_EMPTY,
    _REASON_BRIGHTDATA_NOT_CONFIGURED: _STATE_NOT_CONFIGURED,
    _REASON_BRIGHTDATA_REQUEST_ERROR: "unavailable (trigger rejected)",
    _REASON_BRIGHTDATA_TIMEOUT: "unavailable (poll timeout)",
}


# Tier B — billable: ~173 s per Bright Data collection plus per-record billing, so
# 5 retries would be ~15 min and 5 paid collections. Capped at 2 (ADR-002 #096).
@task(
    name="fetch-youtube-transcripts-batch",
    retries=2,
    retry_delay_seconds=5,
)
async def fetch_transcripts_batch(
    items: list[tuple[str, VideoMetadata]],
) -> tuple[list[_TranscribedItem], list[_FailedItem]]:
    """Run the Bright-Data-primary / Gemini-fallback chain over ONE batch.

    1. Credential gate, BEFORE any billable call: with neither key configured the
       task raises; with only one, it runs on that backend and never constructs
       the other (whose constructor would raise).
    2. Primary: ONE ``BrightDataTranscriptFetcher().fetch_many(urls)`` over ALL
       urls — one Bright Data collection per batch.
    3. Fallback: a SECOND bulk ``fetch_many`` over ONLY the transcript-less slots.
       A batch-WIDE Bright Data failure (missing credentials, trigger rejected,
       poll timeout) makes that subset the whole batch instead of failing the
       task. EVERY fallback is a WARNING naming the reason, the slot count, and
       the Gemini token/cost consequence. A failing GEMINI call is absorbed the
       same way (#095) — the already-fetched Bright Data transcripts must never
       be thrown away and re-billed by a task retry.

    Returns the transcribed slots and the exhausted ones (with a normalized
    ``no_transcript: …`` error each) — one bad video never sinks the batch.
    Network → ``retries=2`` for batch-WIDE failures the chain cannot absorb.
    """

    if not items:
        return [], []

    brightdata_configured = _is_configured(settings.brightdata_api_key)
    gemini_configured = _is_configured(settings.google_api_key)
    if not brightdata_configured and not gemini_configured:
        raise RuntimeError(
            "Neither BRIGHTDATA_API_KEY nor GOOGLE_API_KEY is configured; "
            "see .env.example"
        )

    video_urls = [url for url, _ in items]
    transcripts, brightdata_reason = await _fetch_primary(
        video_urls, configured=brightdata_configured
    )

    gemini_state = _STATE_EMPTY if gemini_configured else _STATE_NOT_CONFIGURED
    missing = [index for index, slot in enumerate(transcripts) if slot is None]
    if missing:
        reason = brightdata_reason or _REASON_NO_BRIGHTDATA_TRANSCRIPT
        if gemini_configured:
            logger.warning(
                "Falling back to Gemini for %d/%d videos (reason=%s) — consumes "
                "Gemini tokens and incurs API cost",
                len(missing),
                len(video_urls),
                reason,
            )
            fallback, gemini_answered = await _fetch_fallback(
                [video_urls[index] for index in missing],
                batch_size=len(video_urls),
            )
            for index, transcript in zip(missing, fallback, strict=True):
                transcripts[index] = transcript
            if not gemini_answered:
                gemini_state = _STATE_FETCH_FAILED
        else:
            logger.warning(
                "No Gemini fallback for %d/%d videos (reason=%s): GOOGLE_API_KEY is "
                "not configured — recording ingest_error rows",
                len(missing),
                len(video_urls),
                reason,
            )

    error = _no_transcript_error(
        brightdata_state=_BRIGHTDATA_STATES[
            brightdata_reason or _REASON_NO_BRIGHTDATA_TRANSCRIPT
        ],
        gemini_state=gemini_state,
    )

    transcribed: list[_TranscribedItem] = []
    failed: list[_FailedItem] = []
    for (url, metadata), transcript in zip(items, transcripts, strict=True):
        if transcript is None:
            logger.warning("No transcript for %s (%s)", url, error)
            failed.append((url, metadata, error))
        else:
            transcribed.append((url, metadata, transcript))
    return transcribed, failed


def _is_configured(api_key: SecretStr | None) -> bool:
    """Whether a credential is present — the ONLY backend switch (ADR-004 §7)."""

    return bool(api_key and api_key.get_secret_value())


async def _fetch_primary(
    video_urls: list[str], *, configured: bool
) -> tuple[list[FetchedTranscript | None], str | None]:
    """Run Bright Data over the batch; return its slots + a batch-wide fallback reason.

    The reason is ``None`` when the collection actually answered (per-slot misses
    are then just transcript-less videos). Every batch-WIDE Bright Data failure is
    absorbed into an all-``None`` result plus its reason, so the chain falls back
    instead of failing the task.
    """

    if not configured:
        return [None] * len(video_urls), _REASON_BRIGHTDATA_NOT_CONFIGURED

    try:
        transcripts = await BrightDataTranscriptFetcher().fetch_many(video_urls)
    except BrightDataConfigurationError as exc:
        return _brightdata_unavailable(
            video_urls, _REASON_BRIGHTDATA_NOT_CONFIGURED, exc
        )
    except BrightDataTimeoutError as exc:
        return _brightdata_unavailable(video_urls, _REASON_BRIGHTDATA_TIMEOUT, exc)
    except BrightDataRequestError as exc:
        return _brightdata_unavailable(
            video_urls, _REASON_BRIGHTDATA_REQUEST_ERROR, exc
        )

    return list(transcripts), None


async def _fetch_fallback(
    video_urls: list[str], *, batch_size: int
) -> tuple[list[FetchedTranscript | None], bool]:
    """Run Gemini over the transcript-less slots; report whether it ANSWERED.

    Any ``Exception`` escaping the bulk ``fetch_many`` is absorbed into empty
    slots plus ``False``, exactly like a batch-WIDE Bright Data failure. Letting
    it escape instead would fail the task and make Prefect (``retries=2``) re-run
    — and RE-BILL — the Bright Data collection whose transcripts are already in
    hand; the slots Gemini did not rescue become ``no_transcript:`` rows instead,
    so nothing is silently lost.

    ``BaseException`` deliberately still propagates: ``asyncio.CancelledError``
    (a ``BaseException`` since 3.8) and ``KeyboardInterrupt`` mean the RUN is
    going away, not that Gemini is down — swallowing them would write failure
    rows for a batch nobody asked to finish. ``exc_info`` keeps the traceback of
    a genuine programming bug in the Gemini path visible in the logs.
    """

    try:
        transcripts = await GeminiTranscriptFetcher().fetch_many(video_urls)
    except Exception as exc:  # noqa: BLE001 — a dead Gemini must not re-bill BD
        logger.warning(
            "Gemini fallback failed for %d/%d videos (%s: %s) — keeping the "
            "Bright Data transcripts already fetched and recording ingest_error "
            "rows for the rest",
            len(video_urls),
            batch_size,
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return [None] * len(video_urls), False

    return list(transcripts), True


def _brightdata_unavailable(
    video_urls: list[str], reason: str, exc: Exception
) -> tuple[list[FetchedTranscript | None], str]:
    """Log a batch-wide Bright Data failure and turn it into empty slots."""

    logger.warning(
        "Bright Data collection unavailable for %d videos (reason=%s): %s",
        len(video_urls),
        reason,
        exc,
    )
    return [None] * len(video_urls), reason


def _no_transcript_error(*, brightdata_state: str, gemini_state: str) -> str:
    """Build the normalized ``no_transcript: …`` error naming the chain that ran."""

    if brightdata_state == _STATE_EMPTY and gemini_state == _STATE_EMPTY:
        return "no_transcript: brightdata + gemini both returned empty"
    return f"no_transcript: brightdata {brightdata_state}; gemini {gemini_state}"


@task(name="build-youtube-batch", retries=0)
async def build_batch(
    transcribed: list[_TranscribedItem],
    failed: list[_FailedItem],
    user_id: PydanticObjectId,
) -> list[Document]:
    """Pure map "transcribed + failed slots → Documents" over one batch.

    Transcribed slots run the shared ``build_document`` with the caller's base
    metadata MERGED under the transcript's own (Bright Data's record fields win
    where they are non-``None``, so ``date`` gets the real ``date_posted``); failed
    slots become ``ingest_error`` rows. No network, no DB → ``retries=0``. A slot
    whose URL cannot be re-resolved to an id is skipped defensively (it was already
    canonical, so this is unreachable in practice).
    """

    documents: list[Document] = []
    for url, metadata, transcript in transcribed:
        video_id = extract_video_id(url)
        if video_id is None:  # pragma: no cover — defensive; url is canonical
            continue
        documents.append(
            build_document(
                video_id=video_id,
                metadata=merge_video_metadata(metadata, transcript.metadata),
                transcript=transcript,
                user_id=user_id,
            )
        )

    documents.extend(
        build_failure_document(
            source_uri=source_uri,
            metadata=metadata,
            ingest_error=ingest_error,
            user_id=user_id,
        )
        for source_uri, metadata, ingest_error in failed
    )
    return documents


@task(name="load-youtube-batch", retries=3, retry_delay_seconds=5)
async def load_batch(docs: list[Document]) -> list[Document]:
    """Dedup + persist one batch via a SINGLE isolated gather.

    Awaits the shared ``load_video_document`` per Document and returns the successful,
    non-``None`` subset (duplicates drop as ``None``); a per-element failure is logged
    at WARNING + skipped, NOT propagated — so one bad slot never sinks the batch, and
    a DB failure is never itself written to the DB as a failure row.
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
    *,
    invalid_inputs: list[str] | None = None,
) -> list[Document]:
    """Shared core: "(url, metadata) list → fallback chain → build → load".

    The single tail both YouTube pipelines run, called ONCE per feed/batch:

    1. ``fetch_transcripts_batch`` — the Bright-Data-primary / Gemini-fallback
       chain (both fetchers constructed inside the task), returning the
       transcribed slots plus the exhausted ones.
    2. ``build_batch`` — ``build_document`` per transcribed slot (with the metadata
       merge) and an ``ingest_error`` row per failed slot.
    3. ``load_batch`` — ``load_video_document`` per Document, isolated per element,
       so failure rows get #089's replace-on-retry semantics for free.

    ``invalid_inputs`` are RAW strings that resolved to no video id at all; they
    become ``invalid_url`` rows keyed on the raw string (there is no canonical URL
    for them) and never reach a transcript backend.

    Metadata is the CALLER's responsibility (oEmbed for direct video, feed for RSS):
    this core never fetches metadata itself, so neither metadata source regresses.

    Returns only the genuinely INGESTED Documents — failure rows are persisted but
    are not ingests, so neither pipeline reports them as such.
    """

    transcribed, failed = await fetch_transcripts_batch(items)
    failed = [
        *failed,
        *((raw_input, None, INVALID_URL_ERROR) for raw_input in invalid_inputs or []),
    ]
    documents = await build_batch(transcribed, failed, user_id)
    loaded = await load_batch(documents)
    return [doc for doc in loaded if doc.ingest_error is None]
