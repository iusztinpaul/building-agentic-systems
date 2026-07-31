"""Single-video YouTube pipeline + the shared bulk core BOTH YouTube pipelines run.

Two layers, per the data-module convention (*_pipeline.py holds Prefect, core
files hold logic):

* **Shared bulk core** — the transcript fallback chain "(url, metadata) list →
  Bright Data bulk fetch → Gemini fallback over ONLY the transcript-less slots →
  build → load" (ADR-004, Decision 1: no per-path branching), run identically by
  the single-video MCP flow below and the RSS/offline batch flow
  (``youtube_pipeline_batch``) via ``_batch_build_and_load``. It lives HERE, not in
  ``youtube.py``, because its ``@task``s belong in a pipeline file and both
  transcript fetchers import ``youtube.py`` (a move would be a circular import).

  Both fetchers are constructed INSIDE the task body, never passed in, so a
  client-holding unpicklable object is never a Prefect task input. Credential
  PRESENCE is the only backend switch: an unconfigured backend is never
  constructed (both constructors raise on a missing key), and with NEITHER
  configured the task raises up-front, before any billable call.

* **Single-video path** — ``_resolve_video_item`` enriches one URL via oEmbed
  (``fetch_oembed_metadata`` + ``parse_oembed_metadata``) into ``(canonical_url,
  VideoMetadata)`` — the direct-video metadata source, reused by the unified
  batch (``youtube_pipeline_batch.ingest_youtube_batch``) for its single-video
  entries. ``_ingest_youtube_video_one`` is the plain async core (resolve →
  ``_batch_build_and_load``); ``ingest_youtube_video`` is a THIN @flow wrapper
  used ONLY by the MCP URL router
  (``tree.data.online_pipeline._ingest_youtube_video``), so a single-URL ingest
  still gets its own Prefect flow run + Opik trace.
"""

from __future__ import annotations

import logging

from beanie import PydanticObjectId
from prefect import flow, task
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
    canonical_video_url,
    extract_video_id,
    fetch_oembed_metadata,
    load_video_document,
    parse_oembed_metadata,
)
from tree.entities.documents import Document

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared bulk core — the transcript fallback chain (tasks + orchestration)
# ---------------------------------------------------------------------------

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


async def build_batch(
    transcribed: list[_TranscribedItem],
    failed: list[_FailedItem],
    user_id: PydanticObjectId,
) -> list[Document]:
    """Pure map "transcribed + failed slots → Documents" over one batch.

    Transcribed slots run the shared ``build_document`` with the caller's base
    metadata MERGED under the transcript's own (Bright Data's record fields win
    where they are non-``None``, so ``date`` gets the real ``date_posted``); failed
    slots become ``ingest_error`` rows. No network, no DB → a PLAIN function, not
    a ``@task`` (ADR-002 amendment #097: a pure map gains nothing from task-hood).
    A slot whose URL cannot be re-resolved to an id is skipped defensively (it was
    already canonical, so this is unreachable in practice).
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


async def _batch_build_and_load(
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


# ---------------------------------------------------------------------------
# Single-video path — oEmbed resolve + thin MCP flow
# ---------------------------------------------------------------------------


async def _resolve_video_item(
    video_url: str,
) -> tuple[str, VideoMetadata] | None:
    """Resolve a pasted URL to ``(canonical_url, oEmbed VideoMetadata)`` or ``None``.

    Returns ``None`` (with a WARNING) when the input cannot be resolved to a video id.
    Otherwise canonicalises the URL and enriches metadata via the per-video oEmbed
    round-trip (``fetch_oembed_metadata`` + ``parse_oembed_metadata``) — the
    direct-video metadata source, kept distinct from the RSS feed-metadata path.
    """

    video_id = extract_video_id(video_url)
    if video_id is None:
        logger.warning("Could not resolve video id from input: %s", video_url)
        return None

    canonical_url = canonical_video_url(video_id)
    payload = await fetch_oembed_metadata(canonical_url)
    metadata = parse_oembed_metadata(payload, video_id=video_id)
    return canonical_url, metadata


# Tier F — free replay (one oEmbed HTTP GET). Task-grain retry because the
# single-video flow deliberately carries NO retries (a flow replay would re-bill
# the Bright Data transcript collection, ADR-002 #096 rule 3c) — this is the one
# network hop before the billable core, so it gets its own durability slot
# (amendment #097). The BATCH path calls the plain ``_resolve_video_item`` under
# ``gather_isolated`` instead, keeping its task-run count constant per shard.
@task(name="resolve-youtube-video", retries=3, retry_delay_seconds=5)
async def resolve_video(video_url: str) -> tuple[str, VideoMetadata] | None:
    return await _resolve_video_item(video_url)


def _partition_video_inputs(video_urls: list[str]) -> tuple[list[str], list[str]]:
    """Split loose video inputs into resolvable URLs and RAW unresolvable ones.

    The same "can this be resolved to a video id?" decision `_resolve_video_item`
    makes, hoisted so the BATCH path can hand its unresolvable inputs to the core
    as ``invalid_url`` ingest_error rows (ADR-004 §6) instead of dropping them —
    a `None` from an isolated gather carries no clue about WHICH input it was.
    """

    resolvable: list[str] = []
    invalid: list[str] = []
    for video_url in video_urls:
        if extract_video_id(video_url) is None:
            logger.warning("Could not resolve video id from input: %s", video_url)
            invalid.append(video_url)
        else:
            resolvable.append(video_url)
    return resolvable, invalid


async def _ingest_youtube_video_one(
    video_url: str,
    user_id: PydanticObjectId,
) -> Document | None:
    """Ingest a SINGLE video via the shared bulk core (plain async core, NO decorators).

    Resolves the id → canonical URL → per-video oEmbed metadata, then runs the SHARED
    ``_batch_build_and_load`` over the one-item list (the transcript fallback chain +
    build + load). Shared by the thin MCP flow; the batch path calls the shared core
    directly with the whole URL list instead. Returns the persisted Document, or
    ``None`` for an unresolvable id / missing transcript / duplicate.

    An unresolvable input still runs the core — over ZERO items and the raw string as
    an ``invalid_url`` failure row, so the attempt is persisted, inspectable data
    rather than a WARNING that scrolls away. No transcript backend is touched.
    """

    resolved = await resolve_video(video_url)
    if resolved is None:
        await _batch_build_and_load([], user_id, invalid_inputs=[video_url])
        return None

    ingested = await _batch_build_and_load([resolved], user_id)
    return ingested[0] if ingested else None


@flow(name="ingest-youtube-video-etl", log_prints=True, validate_parameters=False)
async def ingest_youtube_video(
    video_url: str,
    user_id: PydanticObjectId,
) -> Document | None:
    """Thin MCP-only @flow: ingest ONE YouTube video via the core.

    The MCP ``ingest_url`` router (``tree.data.online_pipeline._ingest_youtube_video``) calls
    this so single-URL ingest still gets its own Prefect flow run + Opik trace. The
    BATCH path does NOT call this — it runs the shared bulk core directly.

    NO ``retries`` here, deliberately (ADR-002 amendment #096, rules 3c + 5): the core
    delegates to ``_batch_build_and_load``, whose tasks already retry
    (``fetch-youtube-transcripts-batch`` 2, ``load-youtube-batch`` 3). Adding a flow
    retry would STACK on those AND replay the billable Bright Data collection
    (~173 s + per-record billing) that #095 works to never pay twice.
    """

    return await _ingest_youtube_video_one(video_url, user_id)
