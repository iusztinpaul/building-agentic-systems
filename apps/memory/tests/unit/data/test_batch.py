"""Unit tests for ``tree.data.batch`` — the shared per-element isolation helper (#079).

``gather_isolated`` runs an async unit-of-work over each element of a batch under a
SINGLE ``asyncio.gather(return_exceptions=True)``: a per-element failure is logged at
WARNING and the element is DROPPED, the helper returns the successful, non-``None``
subset plus the failure count. It NEVER propagates one element's failure — only a
batch-WIDE failure (raised outside the gather) would hard-fail the calling task, which
Prefect then retries idempotently. This is the shape #078 inlined and #079 lifts up now
that it recurs 4+ times (substack RSS load + article extract + article load + arxiv
load).
"""

import logging

from prefect import flow, task

from tree.data.batch import (
    ItemFailureTally,
    gather_isolated,
    item_failures_message,
    parse_items_failed,
    track_item_failures,
)
from tree.flow_runs import PartialIngestError


async def _fail_on_two(x: int) -> int:
    if x == 2:
        raise RuntimeError("boom")
    return x


@task
async def _isolated_task(items: list[int]) -> list[int]:
    results, _ = await gather_isolated(items, _fail_on_two)
    return results


@flow
async def _isolated_flow(items: list[int]) -> list[int]:
    """A leaf batch flow: one feed-level gather in the BODY, one inside a task."""

    await gather_isolated(items, _fail_on_two)
    return await _isolated_task(items)


@flow
async def _flow_raising(message: str) -> None:
    raise PartialIngestError(message)


class TestGatherIsolated:
    async def test_returns_all_successes_with_zero_failures(self) -> None:
        async def _work(x: int) -> int:
            return x * 2

        results, failures = await gather_isolated([1, 2, 3], _work)

        assert results == [2, 4, 6]
        assert failures == 0

    async def test_drops_none_results(self) -> None:
        async def _work(x: int) -> int | None:
            return None if x == 2 else x

        results, failures = await gather_isolated([1, 2, 3], _work)

        # ``None`` (e.g. a dedup skip) is dropped but is NOT counted as a failure.
        assert results == [1, 3]
        assert failures == 0

    async def test_isolates_one_element_failure(self) -> None:
        async def _work(x: int) -> int:
            if x == 2:
                raise RuntimeError("boom")
            return x

        # The raise is caught by gather(return_exceptions=True); NOT propagated.
        results, failures = await gather_isolated([1, 2, 3], _work)

        assert results == [1, 3]
        assert failures == 1

    async def test_all_elements_failing_returns_empty(self) -> None:
        async def _work(x: int) -> int:
            raise RuntimeError("boom")

        results, failures = await gather_isolated([1, 2, 3], _work)

        assert results == []
        assert failures == 3

    async def test_empty_batch_returns_empty(self) -> None:
        async def _work(x: int) -> int:
            raise AssertionError("must not be called for an empty batch")

        results, failures = await gather_isolated([], _work)

        assert results == []
        assert failures == 0

    async def test_logs_a_warning_per_failure(self, caplog) -> None:
        async def _work(x: int) -> int:
            if x == 2:
                raise RuntimeError("boom")
            return x

        with caplog.at_level(logging.WARNING):
            await gather_isolated([1, 2, 3], _work)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    async def test_preserves_input_order(self) -> None:
        async def _work(x: int) -> int:
            return x

        results, _ = await gather_isolated([3, 1, 2], _work)

        # Order tracks the input list, not completion order.
        assert results == [3, 1, 2]


class TestItemFailureTally:
    """Isolated is not forgotten (#174): the data worker counts dropped items."""

    async def test_counts_dropped_elements_and_keeps_the_first_error(self) -> None:
        with track_item_failures() as tally:
            await gather_isolated([1, 2, 3], _fail_on_two)
            await gather_isolated([2, 2], _fail_on_two)

        assert tally == ItemFailureTally(count=3, first_error="RuntimeError: boom")

    async def test_a_clean_batch_leaves_the_tally_at_zero(self) -> None:
        with track_item_failures() as tally:
            await gather_isolated([1, 3], _fail_on_two)

        assert tally.count == 0
        assert tally.first_error is None

    async def test_without_an_active_tally_the_helper_is_unchanged(self) -> None:
        results, failures = await gather_isolated([1, 2, 3], _fail_on_two)

        assert results == [1, 3]
        assert failures == 1

    async def test_the_tally_closes_with_its_block(self) -> None:
        with track_item_failures() as tally:
            pass
        await gather_isolated([2], _fail_on_two)

        # A failure after the block belongs to no worker run.
        assert tally.count == 0

    async def test_counts_through_a_prefect_task_and_an_inline_subflow(self) -> None:
        """The real call shape: gathers inside @task bodies and leaf @flow bodies."""

        with track_item_failures() as tally:
            await _isolated_flow([1, 2, 3])

        assert tally.count == 2


class TestItemFailuresMessage:
    """The worker formats it, the coordinator parses it — across a process hop."""

    def test_the_message_names_the_count_and_the_first_error(self) -> None:
        tally = ItemFailureTally(count=3, first_error="ConnectError: dns")

        assert item_failures_message(tally) == (
            "items_failed=3; first: ConnectError: dns"
        )

    async def test_the_count_survives_prefect_s_failed_state_wrapping(self) -> None:
        message = item_failures_message(ItemFailureTally(count=3, first_error="E: x"))

        state = await _flow_raising(message, return_state=True)

        assert state.is_failed()
        assert parse_items_failed(state.message) == 3

    def test_an_unrelated_failure_message_carries_no_count(self) -> None:
        assert parse_items_failed("flow run finished in state Crashed") is None

    def test_a_crash_that_merely_mentions_the_token_carries_no_count(self) -> None:
        message = "flow run finished in state Crashed (OOM while items_failed=0)"

        assert parse_items_failed(message) is None

    def test_a_zero_count_is_not_a_partial_ingest(self) -> None:
        message = (
            "flow run finished in state Failed (Flow run encountered an exception: "
            "PartialIngestError: items_failed=0; first: None)"
        )

        assert parse_items_failed(message) is None

    def test_the_raw_worker_message_parses(self) -> None:
        error = PartialIngestError(
            item_failures_message(ItemFailureTally(count=2, first_error="E: x"))
        )

        assert parse_items_failed(f"{type(error).__name__}: {error}") == 2


class TestFirstErrorDescription:
    """An exception with no message still tells the operator which item failed."""

    async def test_an_empty_message_names_the_type_and_the_item(self) -> None:
        async def _fail_silently(x: str) -> str:
            raise TimeoutError

        item = "source-" + "x" * 300

        with track_item_failures() as tally:
            await gather_isolated([item], _fail_silently)

        # The item repr ('source-xxx…', quotes included) is cut at 200 characters.
        assert tally.first_error == "TimeoutError on 'source-" + "x" * 192
