"""The ONE place that knows how to wait out a Modal cold start (ADR-009 §11).

Measured live on 2026-09-20 (`ep-qwen3-embedding-4b`): a scaled-to-zero Modal
server answers ``GET /health`` with **HTTP 503 in about a second** — it never
holds the request — and the polling itself is what boots the container. So a
single GET with a long timeout cannot work: it fails in ~1 s and the timeout
never helps. This module polls instead, on a bounded schedule, until the server
answers 200 or the deadline is spent. The `pulse` codebase, whose design this
ports, measured 503 -> 200 at t = 113 s and a slowest boot of 199 s (a 35B LLM).

**Warm at use, not at t0.** Modal scales a container back to zero after its idle
window, so a successful poll is a fact with an expiry date: `pulse` measured an
endpoint warmed at 09:34 answering 503 to the first ten real calls at 09:40 —
ten failed documents. :class:`WarmGate` therefore owns ``warmed`` as a HINT: a
call-time 5xx or connection error (:data:`COLD_CALL_ERRORS`) flips it back,
re-warms ONCE behind the same single-flight lock and retries the call ONCE.

Two deliberate boundaries:

* **No new exception type.** :class:`~tree.models.exceptions.ModelError` means
  configuration — waiting will not help; :class:`ExtractionError` means
  transient. That is the split the Modal client already had, and no caller
  would branch on a third type.
* **No ``tree.config`` import.** The deadline is a PARAMETER
  (``modal.warmup_deadline_s`` is read by the callers), and no ``modal`` SDK
  import either — so the MCP boot path never pays for this module.

``sleep``, ``clock`` and ``fetch`` are injected, which is what lets the unit
suite exercise a 600 s budget in milliseconds with zero network.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterator

import aiohttp
import openai

from tree.models.exceptions import ExtractionError, ModelError

logger = logging.getLogger(__name__)

# Per-GET timeout. A cold server answers in ~1 s, so 10 s covers proxy jitter,
# and a hung GET burns at most 10 s of the budget as a retryable transport
# error. The DEADLINE is enforced by the loop clock, never by this timeout.
POLL_TIMEOUT_S = 10.0

# Sleep between polls: 5, 7.5, 11.25, then 15 flat. The first GET fires
# immediately, so polls land at t ~ 0, 5, 12.5, 23.75, 38.75, … — ten polls
# inside the measured 113 s boot — and the 15 s cap bounds how long a ready
# container waits to be noticed.
INITIAL_INTERVAL_S = 5.0
BACKOFF_FACTOR = 1.5
MAX_INTERVAL_S = 15.0

# What a cold container looks like AT CALL TIME — the twin of the poll's
# classification below. The OpenAI SDK raises ``InternalServerError`` for EVERY
# status >= 500 and ``APIConnectionError`` (incl. its ``APITimeoutError``
# subclass) for refused connections and timeouts. Nothing else: a 4xx — 429
# included — never becomes 200 by waiting, so spending the deadline on it only
# wastes the operator's time.
COLD_CALL_ERRORS: tuple[type[Exception], ...] = (
    openai.InternalServerError,
    openai.APIConnectionError,
)

# (url, headers, per-GET timeout) -> HTTP status. The test seam: the default
# does one `aiohttp` GET, and every unit test injects its own.
Fetch = Callable[[str, dict[str, str], float], Awaitable[int]]

# What the deadline message reports when the budget was already spent before
# the first GET (``deadline_s <= 0``). Every loop iteration overwrites it, so
# this is the ONLY path that reaches it — `modal.warmup_deadline_s` is `ge=1`,
# but `poll_health` takes any float and must not name an unset last result.
_NO_RESPONSE = "no response"


async def _get_status(url: str, headers: dict[str, str], timeout_s: float) -> int:
    """One authenticated GET; the status, or an ``aiohttp`` / timeout error."""

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as response:
            return response.status


async def poll_health(
    url: str,
    headers: dict[str, str],
    *,
    deadline_s: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    fetch: Fetch = _get_status,
) -> float:
    """Seconds until ``url`` answered 200, polling until the deadline is spent.

    The first GET fires IMMEDIATELY (a warm server costs no wait), then the
    backoff of :data:`INITIAL_INTERVAL_S` x :data:`BACKOFF_FACTOR` capped at
    :data:`MAX_INTERVAL_S`, with the last sleep clipped to the time left.

    **Closed three-way classification** — a 4xx never becomes 200 by waiting:

    1. ``200`` -> warm, return the elapsed seconds.
    2. ``5xx``, a refused connection, a timeout -> still booting, keep polling.
    3. anything else — 401/403 (a bad **Proxy token**), 404 (a wrong URL), any
       other status, any other ``aiohttp`` error (redirect loop, payload
       decoding) -> give up after ONE attempt.

    Args:
        url: The full ``/health`` URL to poll (it is the only thing logged —
            never the headers, which carry the Proxy token).
        headers: Sent with every GET; ``Authorization: Bearer <proxy token>``.
        deadline_s: Total budget, enforced by ``clock``.

    Raises:
        ModelError: class 3 — configuration, so no retry can help.
        ExtractionError: the deadline was spent while the server was still
            cold; ``status_code`` is the last HTTP status, or ``None`` when the
            last result was a transport error.
    """

    start = clock()
    intervals = _schedule()
    last_label, last_status = _NO_RESPONSE, None
    logger.info("Warming %s — polling for up to %.0fs", url, deadline_s)

    while clock() - start < deadline_s:
        try:
            status = await fetch(url, headers, POLL_TIMEOUT_S)
        except (TimeoutError, aiohttp.ClientConnectionError) as exc:
            # A refused connection or a read timeout is what booting looks
            # like. ``asyncio.TimeoutError`` IS ``TimeoutError`` on 3.11+.
            last_label, last_status = _describe(exc), None
        except aiohttp.ClientError as exc:
            # A redirect loop or a decoding error is a BROKEN endpoint, the
            # same fail-fast class as a 403 — and catching it here keeps a raw
            # aiohttp error from escaping this module's two exception types.
            raise ModelError(
                f"Health poll of {url} raised {_describe(exc)} — not a cold "
                "start, giving up after 1 attempt."
            ) from exc
        else:
            if status == 200:
                elapsed = clock() - start
                logger.info("Warm: %s answered HTTP 200 after %.0fs", url, elapsed)
                return elapsed
            if not 500 <= status < 600:
                raise ModelError(_fail_fast_message(url, status))
            last_label, last_status = f"HTTP {status}", status

        # One line per poll: a 2-10 minute boot must never look hung.
        logger.info(
            "Still cold (%s) at %s — %.0fs/%.0fs",
            last_label,
            url,
            clock() - start,
            deadline_s,
        )
        wait = max(0.0, min(next(intervals), deadline_s - (clock() - start)))
        if wait > 0:
            await sleep(wait)

    raise ExtractionError(
        f"Health poll of {url} gave up after {clock() - start:.0f}s (deadline "
        f"{deadline_s:.0f}s); last result: {last_label}",
        status_code=last_status,
    )


class WarmGate:
    """One server's warm state: the single flight, the hint, and the re-warm.

    COMPOSED by the Modal clients rather than inherited (ADR-009 §11), so there
    is no implicit attribute contract on them and the recovery is testable with
    a fake ``warm`` and fake call factories, with no SDK in the picture.

    ``warm`` is the caller's ALL-OR-NOTHING body — for
    :class:`~tree.models.modal_embedding.ModalEmbeddingModel` it is the URL
    lookup, the health poll, the served-model discovery and the client
    construction — because here the URL itself is discovered at first use. It
    runs again, whole, on a cold-again re-warm and on an event-loop change.
    """

    def __init__(self, warm: Callable[[], Awaitable[None]], label: str) -> None:
        self._warm = warm
        self._label = label
        #: A HINT, not a guarantee: true since the last successful warm, until
        #: a call proves otherwise. Skipping the poll is what keeps a
        #: 200-document run from paying for 200 polls.
        self.warmed = False
        self._lock = asyncio.Lock()
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    async def ensure_warm(self) -> None:
        """Run ``warm`` — at most once per warm period, however many callers.

        One instance serves a whole window of concurrent callers, so the flag
        is re-checked UNDER the lock: the first caller warms and the rest of
        the burst wait for its answer. The flag is set only AFTER ``warm``
        returned, so a failure leaves the gate cold and the next call retries
        the whole body.
        """

        lock = self._loop_lock()
        if self.warmed:
            return
        async with lock:
            if self.warmed:
                return
            await self._warm()
            self.warmed = True

    async def call[T](self, fn: Callable[[], Awaitable[T]]) -> T:
        """Run ``fn`` warm; on a cold answer re-warm ONCE and retry it ONCE.

        Every other exception propagates UNCHANGED — the caller's ``except``
        already turns it into its own error, and this gate must not widen what
        a 4xx means.

        Raises:
            ExtractionError: the second answer was cold too, or the re-warm
                itself failed on a spent deadline (prefixed, retryable).
            ModelError: the re-warm failed fast (a bad token, a wrong URL).
        """

        await self.ensure_warm()
        try:
            return await fn()
        except COLD_CALL_ERRORS as exc:
            label = _cold_label(exc)
            # ONE line per cold period, not one per caller: eight concurrent
            # callers all see the same 503 and share ONE re-warm, so the flag
            # that gates the re-warm gates the WARNING too. Nothing may be
            # awaited between reading the flag and clearing it.
            if self.warmed:
                logger.warning(
                    "Cold again: %s answered %s — re-warming once", self._label, label
                )
            self.warmed = False
            await self._rewarm(label)
            return await self._retry(fn)

    async def _rewarm(self, label: str) -> None:
        """The one re-warm, behind the same lock; its failure names the cause."""

        try:
            await self.ensure_warm()
        except ExtractionError as exc:
            raise ExtractionError(
                f"re-warm of {self._label} after {label} failed: {exc}",
                status_code=exc.status_code,
            ) from exc
        except ModelError as exc:
            raise ModelError(
                f"re-warm of {self._label} after {label} failed: {exc}"
            ) from exc

    async def _retry[T](self, fn: Callable[[], Awaitable[T]]) -> T:
        """The one retry after a successful re-warm; a second cold answer ends it."""

        try:
            result = await fn()
        except COLD_CALL_ERRORS as exc:
            raise ExtractionError(
                f"{self._label} still answered {_cold_label(exc)} after one re-warm",
                status_code=_cold_status(exc),
            ) from exc
        logger.info("Warm again: %s answered after one re-warm", self._label)
        return result

    def _loop_lock(self) -> asyncio.Lock:
        """The lock of the RUNNING loop, re-made — cold — when that loop changes.

        One instance outlives an ``asyncio.run``: scripts call it more than
        once, Prefect may run a task on another loop, and the MCP server keeps
        one instance for the process lifetime. An ``asyncio.Lock`` binds to the
        loop of its first contended acquire and raises "bound to a different
        event loop" on the next one. The hint is reset with the lock because
        whatever the warm built (an HTTP pool) belongs to the old loop.
        """

        loop = asyncio.get_running_loop()
        if self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
            self.warmed = False
        return self._lock


def _schedule() -> Iterator[float]:
    interval = INITIAL_INTERVAL_S
    while True:
        yield interval
        interval = min(interval * BACKOFF_FACTOR, MAX_INTERVAL_S)


def _fail_fast_message(url: str, status: int) -> str:
    """Why waiting cannot help — with the operator's next step for a 401/403."""

    message = (
        f"Health poll of {url} returned HTTP {status} — not a cold start, "
        "giving up after 1 attempt."
    )
    if status in (401, 403):
        message += " Check MODAL_PROXY_TOKEN_ID and MODAL_PROXY_TOKEN_SECRET in .env."
    return message


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _cold_label(exc: Exception) -> str:
    """``HTTP 503`` when the SDK carries a status, else the exception's class."""

    status = _cold_status(exc)
    return f"HTTP {status}" if status is not None else type(exc).__name__


def _cold_status(exc: Exception) -> int | None:
    return exc.status_code if isinstance(exc, openai.APIStatusError) else None
