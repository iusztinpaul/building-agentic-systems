"""Unit tests for ``tree.models.modal_warmup`` — the ONE poller that waits out
a Modal cold start and the :class:`WarmGate` both Modal clients compose
(ADR-009 §11, the **Warm gate** glossary entry).

Nothing here touches the network or sleeps for real: ``fetch``, ``sleep`` and
``clock`` are injected, and the fake clock only moves when the injected sleep
is awaited — so a 600 s budget is exercised in milliseconds. The gate is driven
with FAKE warm coroutines and FAKE call factories: it is the shared mechanism,
so it must prove itself without either client (or the OpenAI SDK's request
path) in the picture.

The **Proxy token** is always the FAKE pair ``wk-1`` / ``ws-2`` — ``make``
exports the developer's real ``.env`` into the test process, so a test that
read the real token would both leak it and pass for the wrong reason.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from typing import Any

import aiohttp
import httpx
import openai
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from tree.models.exceptions import ExtractionError, ModelError
from tree.models.modal_warmup import (
    BACKOFF_FACTOR,
    COLD_CALL_ERRORS,
    INITIAL_INTERVAL_S,
    MAX_INTERVAL_S,
    POLL_TIMEOUT_S,
    WarmGate,
    poll_health,
)

_TOKEN = "wk-1.ws-2"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}
_URL = "https://acme--ep-tree-voyage-4-nano-server.modal.run/health"
_CALL_URL = "https://acme--ep-tree-voyage-4-nano-server.modal.run/v1/embeddings"
_LABEL = "ep-tree-voyage-4-nano"


# --- doubles ----------------------------------------------------------------


class _FakeClock:
    """A monotonic clock that only moves when the injected sleep is awaited.

    This is what turns the 600 s budget into a millisecond test: the poll's
    deadline is enforced by THIS clock, never by a wall-clock timeout.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _Fetch:
    """Serves ``outcomes`` in order, then repeats the last one for ever.

    An ``int`` is an HTTP status; an ``Exception`` is raised. Repeating the last
    outcome is what "a container that never finishes booting" looks like.
    """

    def __init__(self, *outcomes: int | Exception, clock: _FakeClock | None) -> None:
        self._outcomes = list(outcomes)
        self._clock = clock
        self.calls: list[tuple[str, dict[str, str], float]] = []
        self.times: list[float] = []

    async def __call__(
        self, url: str, headers: dict[str, str], timeout_s: float
    ) -> int:
        self.calls.append((url, headers, timeout_s))
        if self._clock is not None:
            self.times.append(self._clock.now)
        outcome = (
            self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _too_many_redirects() -> aiohttp.TooManyRedirects:
    """A broken endpoint, not a booting one — a ``ClientResponseError``."""

    url = URL(_URL)
    info = aiohttp.RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url)
    return aiohttp.TooManyRedirects(info, ())


def _cold(status: int = 503) -> openai.InternalServerError:
    """The SDK error a scaled-to-zero container produces: every 5xx maps here."""

    return openai.InternalServerError(
        "boom",
        response=httpx.Response(status, request=httpx.Request("POST", _CALL_URL)),
        body=None,
    )


def _refused() -> openai.APIConnectionError:
    """The SDK error for a container that is not accepting connections yet."""

    return openai.APIConnectionError(request=httpx.Request("POST", _CALL_URL))


def _timed_out() -> openai.APITimeoutError:
    """A timeout IS a cold start — ``APITimeoutError`` is an ``APIConnectionError``."""

    return openai.APITimeoutError(request=httpx.Request("POST", _CALL_URL))


def _fatal(error: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    """A 4xx the gate must never treat as cold: it never becomes 200 by waiting."""

    return error(
        "nope",
        response=httpx.Response(status, request=httpx.Request("POST", _CALL_URL)),
        body=None,
    )


class _Warm:
    """A fake single-flight warm body: counts, suspends, optionally raises.

    It ALWAYS suspends (``asyncio.sleep(0)``): an awaitable that never yields
    lets a burst of callers run one at a time, which would make a
    check-then-act race invisible.
    """

    def __init__(self, *errors: Exception | None) -> None:
        self.calls = 0
        self._errors = list(errors)

    async def __call__(self) -> None:
        self.calls += 1
        await asyncio.sleep(0)
        if self._errors:
            error = self._errors.pop(0)
            if error is not None:
                raise error


class _Call:
    """A call factory that raises ``outcomes`` in order, then answers ``result``."""

    def __init__(self, *outcomes: Exception, result: str = "ok", yields: int = 0):
        self._outcomes = list(outcomes)
        self._yields = yields
        self.result = result
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        for _ in range(self._yields):
            await asyncio.sleep(0)
        if self._outcomes:
            raise self._outcomes.pop(0)
        return self.result


class _FakeResponse:
    """One canned aiohttp response, usable as an async context manager."""

    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    """Records how the default ``fetch`` builds its session and its GET."""

    def __init__(self, recorder: dict[str, Any], **kwargs: Any) -> None:
        self._recorder = recorder
        recorder["session_kwargs"] = kwargs

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self._recorder["get"] = {"url": url, **kwargs}
        return _FakeResponse(200)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


# --- the poller -------------------------------------------------------------


class TestConstants:
    def test_the_schedule_matches_the_measured_cold_start(self) -> None:
        """10 s per GET (a cold server answers in ~1 s), 5 s x 1.5 capped at 15 s
        — polls at t ~ 0, 5, 12.5, 23.75, 38.75, then every 15 s (ADR-009 §11)."""

        assert POLL_TIMEOUT_S == 10.0
        assert INITIAL_INTERVAL_S == 5.0
        assert BACKOFF_FACTOR == 1.5
        assert MAX_INTERVAL_S == 15.0

    def test_cold_call_errors_are_exactly_the_sdks_5xx_and_connection_errors(
        self,
    ) -> None:
        """The call-time twin of the poll's classification: the SDK raises
        ``InternalServerError`` for every status >= 500 and
        ``APIConnectionError`` (incl. ``APITimeoutError``) for refused
        connections and timeouts. A 429 or any other 4xx is NEVER cold."""

        assert COLD_CALL_ERRORS == (
            openai.InternalServerError,
            openai.APIConnectionError,
        )
        assert issubclass(openai.APITimeoutError, openai.APIConnectionError)


class TestPollHealth:
    async def test_a_booting_server_is_polled_until_it_answers_200(self) -> None:
        """The bug this module exists for: a scaled-to-zero server answers 503
        in ~1 s and boots BECAUSE it is polled."""

        clock = _FakeClock()
        fetch = _Fetch(503, 503, 200, clock=clock)

        elapsed = await poll_health(
            _URL,
            _HEADERS,
            deadline_s=600.0,
            sleep=clock.sleep,
            clock=clock,
            fetch=fetch,
        )

        assert len(fetch.calls) == 3
        assert clock.sleeps == [5.0, 7.5]
        # The first GET fires IMMEDIATELY — a warm server costs no 5 s wait.
        assert fetch.times[0] == 0.0
        assert elapsed == 12.5

    async def test_every_get_carries_the_headers_and_the_per_get_timeout(
        self,
    ) -> None:
        clock = _FakeClock()
        fetch = _Fetch(200, clock=clock)

        await poll_health(
            _URL,
            _HEADERS,
            deadline_s=600.0,
            sleep=clock.sleep,
            clock=clock,
            fetch=fetch,
        )

        assert fetch.calls == [(_URL, _HEADERS, POLL_TIMEOUT_S)]

    async def test_the_backoff_caps_at_fifteen_seconds(self) -> None:
        """Bounded detection latency: once the container is ready, nobody waits
        more than 15 s to notice."""

        clock = _FakeClock()
        fetch = _Fetch(*([503] * 8), 200, clock=clock)

        await poll_health(
            _URL,
            _HEADERS,
            deadline_s=600.0,
            sleep=clock.sleep,
            clock=clock,
            fetch=fetch,
        )

        assert clock.sleeps == [5.0, 7.5, 11.25, 15.0, 15.0, 15.0, 15.0, 15.0]
        assert len(fetch.calls) == 9

    async def test_transport_failures_are_a_booting_container(self) -> None:
        """A refused connection and a read timeout are what booting looks like
        — the same class as a 5xx, not a broken endpoint."""

        clock = _FakeClock()
        fetch = _Fetch(
            aiohttp.ClientConnectionError("connection refused"),
            TimeoutError("timed out"),
            200,
            clock=clock,
        )

        await poll_health(
            _URL,
            _HEADERS,
            deadline_s=600.0,
            sleep=clock.sleep,
            clock=clock,
            fetch=fetch,
        )

        assert len(fetch.calls) == 3

    @pytest.mark.parametrize("status", [401, 403, 404, 302, 418])
    async def test_a_status_that_never_becomes_200_fails_after_one_attempt(
        self, status: int
    ) -> None:
        """Story 2: a wrong **Proxy token** or a wrong URL must cost seconds,
        not the whole 600 s budget — a 4xx never becomes 200 by waiting."""

        clock = _FakeClock()
        fetch = _Fetch(status, clock=clock)

        with pytest.raises(ModelError) as excinfo:
            await poll_health(
                _URL,
                _HEADERS,
                deadline_s=600.0,
                sleep=clock.sleep,
                clock=clock,
                fetch=fetch,
            )

        message = str(excinfo.value)
        # Exactly ModelError: configuration, not the retryable ExtractionError.
        assert type(excinfo.value) is ModelError
        assert _URL in message
        assert f"HTTP {status}" in message
        assert "giving up after 1 attempt" in message
        assert len(fetch.calls) == 1
        assert clock.sleeps == []

    @pytest.mark.parametrize("status", [401, 403])
    async def test_an_auth_failure_names_both_proxy_token_variables(
        self, status: int
    ) -> None:
        clock = _FakeClock()
        fetch = _Fetch(status, clock=clock)

        with pytest.raises(ModelError) as excinfo:
            await poll_health(
                _URL,
                _HEADERS,
                deadline_s=600.0,
                sleep=clock.sleep,
                clock=clock,
                fetch=fetch,
            )

        message = str(excinfo.value)
        assert "MODAL_PROXY_TOKEN_ID" in message
        assert "MODAL_PROXY_TOKEN_SECRET" in message

    @pytest.mark.parametrize(
        "error",
        [_too_many_redirects(), aiohttp.ClientPayloadError("truncated body")],
        ids=["redirect-loop", "payload-error"],
    )
    async def test_a_broken_endpoint_fails_after_one_attempt(
        self, error: Exception
    ) -> None:
        """A redirect loop or a decoding error is a broken endpoint, not a
        booting one — and catching it here keeps a raw ``aiohttp`` error from
        escaping the ``ModelError`` / ``ExtractionError`` contract."""

        clock = _FakeClock()
        fetch = _Fetch(error, clock=clock)

        with pytest.raises(ModelError) as excinfo:
            await poll_health(
                _URL,
                _HEADERS,
                deadline_s=600.0,
                sleep=clock.sleep,
                clock=clock,
                fetch=fetch,
            )

        assert type(excinfo.value) is ModelError
        assert "giving up after 1 attempt" in str(excinfo.value)
        assert len(fetch.calls) == 1

    async def test_a_spent_deadline_is_retryable_and_carries_the_last_status(
        self,
    ) -> None:
        """Story 5: a server that never comes up is a TRANSIENT failure the
        caller may retry — and the budget is never overshot."""

        clock = _FakeClock()
        fetch = _Fetch(503, clock=clock)

        with pytest.raises(ExtractionError) as excinfo:
            await poll_health(
                _URL,
                _HEADERS,
                deadline_s=600.0,
                sleep=clock.sleep,
                clock=clock,
                fetch=fetch,
            )

        message = str(excinfo.value)
        assert excinfo.value.status_code == 503
        assert _URL in message
        assert "gave up after" in message
        assert "last result: HTTP 503" in message
        assert sum(clock.sleeps) <= 600.0
        assert clock.now == 600.0

    async def test_the_last_sleep_is_clipped_to_the_time_left(self) -> None:
        """The deadline is a budget, not a floor: the poll never sleeps past it."""

        clock = _FakeClock()
        fetch = _Fetch(503, clock=clock)

        with pytest.raises(ExtractionError, match="deadline 12s"):
            await poll_health(
                _URL,
                _HEADERS,
                deadline_s=12.0,
                sleep=clock.sleep,
                clock=clock,
                fetch=fetch,
            )

        assert clock.sleeps == [5.0, 7.0]
        assert clock.now == 12.0

    async def test_a_one_second_budget_costs_exactly_one_poll(self) -> None:
        clock = _FakeClock()
        fetch = _Fetch(503, clock=clock)

        with pytest.raises(ExtractionError, match="gave up after"):
            await poll_health(
                _URL,
                _HEADERS,
                deadline_s=1.0,
                sleep=clock.sleep,
                clock=clock,
                fetch=fetch,
            )

        assert len(fetch.calls) == 1

    async def test_a_budget_of_zero_polls_nothing_and_says_so(self) -> None:
        """The one path that reports ``no response``: the budget was spent
        before the first GET. The config knob is ``ge=1``, but the function
        takes any float and must not name a result it never got."""

        clock = _FakeClock()
        fetch = _Fetch(200, clock=clock)

        with pytest.raises(ExtractionError) as excinfo:
            await poll_health(
                _URL,
                _HEADERS,
                deadline_s=0.0,
                sleep=clock.sleep,
                clock=clock,
                fetch=fetch,
            )

        assert "last result: no response" in str(excinfo.value)
        assert excinfo.value.status_code is None
        assert fetch.calls == []

    async def test_a_deadline_spent_on_transport_errors_carries_no_status(
        self,
    ) -> None:
        """``status_code`` is None when the last result was not an HTTP answer —
        a caller branching on 503 must not read a transport failure as one."""

        clock = _FakeClock()
        fetch = _Fetch(aiohttp.ClientConnectionError("refused"), clock=clock)

        with pytest.raises(ExtractionError) as excinfo:
            await poll_health(
                _URL,
                _HEADERS,
                deadline_s=60.0,
                sleep=clock.sleep,
                clock=clock,
                fetch=fetch,
            )

        assert excinfo.value.status_code is None
        assert "ClientConnectionError" in str(excinfo.value)

    async def test_one_line_per_poll_so_a_long_boot_never_looks_hung(
        self, caplog
    ) -> None:
        """Story 1: a 2-10 minute boot prints progress, and the token never
        reaches a log record (the poller logs the URL, never the headers)."""

        clock = _FakeClock()
        fetch = _Fetch(503, 503, 200, clock=clock)

        with caplog.at_level(logging.INFO):
            await poll_health(
                _URL,
                _HEADERS,
                deadline_s=600.0,
                sleep=clock.sleep,
                clock=clock,
                fetch=fetch,
            )

        messages = [record.getMessage() for record in caplog.records]
        assert f"Warming {_URL} — polling for up to 600s" in messages
        cold = [m for m in messages if m.startswith(f"Still cold (HTTP 503) at {_URL}")]
        assert cold == [
            f"Still cold (HTTP 503) at {_URL} — 0s/600s",
            f"Still cold (HTTP 503) at {_URL} — 5s/600s",
        ]
        warm_lines = [
            m for m in messages if m.startswith(f"Warm: {_URL} answered HTTP 200 after")
        ]
        assert len(warm_lines) == 1
        assert not any("Bearer" in message or _TOKEN in message for message in messages)

    async def test_the_default_fetch_sends_the_headers_over_aiohttp(
        self, mocker
    ) -> None:
        """The ONE real HTTP door of this module: a header-carrying GET with a
        per-request timeout, answering with the status and nothing else."""

        recorder: dict[str, Any] = {}
        mocker.patch.object(
            aiohttp,
            "ClientSession",
            lambda **kwargs: _FakeSession(recorder, **kwargs),
        )
        clock = _FakeClock()

        await poll_health(
            _URL, _HEADERS, deadline_s=600.0, sleep=clock.sleep, clock=clock
        )

        assert recorder["session_kwargs"]["headers"] == _HEADERS
        assert recorder["get"]["url"] == _URL
        assert recorder["get"]["timeout"].total == POLL_TIMEOUT_S


# --- the gate ---------------------------------------------------------------


class TestWarmGateEnsureWarm:
    async def test_a_burst_of_first_callers_pays_exactly_one_warm(self) -> None:
        """Story 4: eight concurrent embeds hit a cold server — one URL lookup,
        one poll loop, one ``/v1/models`` call, eight answers."""

        warm = _Warm()
        gate = WarmGate(warm, label=_LABEL)

        await asyncio.gather(*(gate.ensure_warm() for _ in range(8)))

        assert warm.calls == 1
        assert gate.warmed is True

    async def test_a_warm_gate_is_a_fast_path(self) -> None:
        warm = _Warm()
        gate = WarmGate(warm, label=_LABEL)

        await gate.ensure_warm()
        await gate.ensure_warm()

        assert warm.calls == 1

    async def test_a_failed_warm_leaves_the_gate_cold(self) -> None:
        """All-or-nothing: the flag is set only AFTER ``warm`` returned, so a
        failure is retried in full instead of half-initialising the client."""

        warm = _Warm(ExtractionError("gave up after 600s"), None)
        gate = WarmGate(warm, label=_LABEL)

        with pytest.raises(ExtractionError):
            await gate.ensure_warm()
        assert gate.warmed is False

        await gate.ensure_warm()

        assert warm.calls == 2
        assert gate.warmed is True

    async def test_every_waiter_of_a_failed_warm_sees_the_error(self) -> None:
        warm = _Warm(*([ExtractionError("gave up after 600s")] * 8))
        gate = WarmGate(warm, label=_LABEL)

        results = await asyncio.gather(
            *(gate.ensure_warm() for _ in range(8)), return_exceptions=True
        )

        # No waiter may fall through to the call with a half-built client: the
        # flag is re-checked under the lock and it is still cold, so each of
        # them surfaces the failure instead of a "warm" no poll ever proved.
        assert all(isinstance(result, ExtractionError) for result in results)
        assert gate.warmed is False

    async def test_a_cancelled_waiter_leaves_the_gate_usable(self) -> None:
        """A cancelled MCP tool call must not deadlock or poison the instance."""

        warm = _Warm()
        gate = WarmGate(warm, label=_LABEL)
        waiters = [asyncio.create_task(gate.ensure_warm()) for _ in range(3)]
        await asyncio.sleep(0)

        waiters[-1].cancel()
        results = await asyncio.gather(*waiters, return_exceptions=True)

        assert isinstance(results[-1], asyncio.CancelledError)
        assert warm.calls == 1
        assert gate.warmed is True

    async def test_a_cancelled_winner_does_not_leave_the_lock_held(self) -> None:
        """The one holding the lock is cancelled mid-warm: the next caller must
        warm again rather than wait for ever or inherit a half-warm gate."""

        warm = _Warm()
        gate = WarmGate(warm, label=_LABEL)
        winner = asyncio.create_task(gate.ensure_warm())
        await asyncio.sleep(0)

        winner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await winner
        assert gate.warmed is False

        await gate.ensure_warm()

        assert warm.calls == 2
        assert gate.warmed is True


def test_gate_survives_a_second_event_loop() -> None:
    """One instance outlives an ``asyncio.run``: scripts call it more than once,
    Prefect may run a task on another loop, and the MCP server keeps one
    instance for the process lifetime. An ``asyncio.Lock`` binds to the loop of
    its first CONTENDED acquire and raises "bound to a different event loop" on
    the next one — and the warm hint is reset with it, because the client the
    warm built (its HTTP pool) belongs to the old loop.

    A plain ``def``: ``asyncio.run`` cannot be called from inside a running loop.
    """

    warm = _Warm()
    gate = WarmGate(warm, label=_LABEL)

    async def burst() -> None:
        await asyncio.gather(*(gate.ensure_warm() for _ in range(2)))

    for _ in range(2):
        asyncio.run(burst())

    assert warm.calls == 2


class TestWarmGateCall:
    async def test_a_cold_answer_re_warms_once_and_retries_once(self, caplog) -> None:
        """Story 3: Modal scaled the server to zero mid-run, so the warm flag is
        a HINT with an expiry — the call-time 503 is what proves it stale."""

        warm = _Warm()
        gate = WarmGate(warm, label=_LABEL)
        call = _Call(_cold())

        with caplog.at_level(logging.INFO):
            result = await gate.call(call)

        assert result == "ok"
        assert call.calls == 2
        assert warm.calls == 2
        assert gate.warmed is True
        messages = [record.getMessage() for record in caplog.records]
        assert f"Cold again: {_LABEL} answered HTTP 503 — re-warming once" in messages
        assert f"Warm again: {_LABEL} answered after one re-warm" in messages

    @pytest.mark.parametrize(
        "error", [_refused(), _timed_out()], ids=["refused", "timeout"]
    )
    async def test_a_connection_error_is_a_cold_start_too(
        self, error: Exception
    ) -> None:
        """A container that refuses the connection, or answers too slowly, is
        booting — not broken."""

        warm = _Warm()
        gate = WarmGate(warm, label=_LABEL)
        call = _Call(error)

        assert await gate.call(call) == "ok"
        assert (warm.calls, call.calls) == (2, 2)

    async def test_eight_concurrent_cold_callers_log_one_warning(self, caplog) -> None:
        """ONE line per cold period, not one per caller: the flag that gates the
        re-warm gates the WARNING too."""

        warm = _Warm()
        gate = WarmGate(warm, label=_LABEL)
        calls = [_Call(_cold(), yields=1) for _ in range(8)]
        await gate.ensure_warm()

        with caplog.at_level(logging.WARNING):
            results = await asyncio.gather(*(gate.call(call) for call in calls))

        assert results == ["ok"] * 8
        assert warm.calls == 2
        assert sum(call.calls for call in calls) == 16
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    async def test_cold_on_the_retry_too_raises_a_retryable_error(self) -> None:
        warm = _Warm()
        gate = WarmGate(warm, label=_LABEL)
        call = _Call(_cold(), _cold())

        with pytest.raises(ExtractionError) as excinfo:
            await gate.call(call)

        message = str(excinfo.value)
        assert f"{_LABEL} still answered HTTP 503 after one re-warm" == message
        assert excinfo.value.status_code == 503
        assert call.calls == 2

    async def test_a_failed_re_warm_is_prefixed_and_keeps_its_type(self) -> None:
        """The re-warm's own failure is what the caller must read — a spent
        deadline stays retryable and keeps its status code."""

        warm = _Warm(None, ExtractionError("gave up after 600s", status_code=503))
        gate = WarmGate(warm, label=_LABEL)
        call = _Call(_cold())

        with pytest.raises(ExtractionError) as excinfo:
            await gate.call(call)

        assert str(excinfo.value) == (
            f"re-warm of {_LABEL} after HTTP 503 failed: gave up after 600s"
        )
        assert excinfo.value.status_code == 503
        assert call.calls == 1
        assert gate.warmed is False

    async def test_a_re_warm_that_fails_fast_stays_a_model_error(self) -> None:
        """A 403 during the re-warm is configuration: it must NOT be widened
        into the retryable ExtractionError."""

        warm = _Warm(None, ModelError("returned HTTP 403"))
        gate = WarmGate(warm, label=_LABEL)

        with pytest.raises(ModelError) as excinfo:
            await gate.call(_Call(_cold()))

        assert type(excinfo.value) is ModelError
        assert str(excinfo.value).startswith(f"re-warm of {_LABEL} after HTTP 503")

    @pytest.mark.parametrize(
        ("error", "status"),
        [
            (openai.RateLimitError, 429),
            (openai.BadRequestError, 400),
            (openai.AuthenticationError, 401),
        ],
    )
    async def test_a_4xx_passes_through_unchanged(
        self, error: type[openai.APIStatusError], status: int, caplog
    ) -> None:
        """429 included: waiting out a boot cannot fix a quota, and burning the
        deadline on it only wastes the operator's time."""

        warm = _Warm()
        gate = WarmGate(warm, label=_LABEL)
        call = _Call(_fatal(error, status))

        with caplog.at_level(logging.WARNING):
            with pytest.raises(error):
                await gate.call(call)

        assert (warm.calls, call.calls) == (1, 1)
        assert gate.warmed is True
        assert caplog.records == []

    async def test_a_non_sdk_exception_passes_through_unchanged(self) -> None:
        """The gate only knows cold starts; everything else is the caller's."""

        gate = WarmGate(_Warm(), label=_LABEL)
        call = _Call(ValueError("bad payload"))

        with pytest.raises(ValueError, match="bad payload"):
            await gate.call(call)
        assert call.calls == 1


def test_modal_warmup_does_not_import_modal() -> None:
    """The poller is HTTP + config-free: importing it must not pull in the
    ``modal`` SDK, so the MCP boot path never pays for it."""

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, tree.models.modal_warmup; assert 'modal' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
