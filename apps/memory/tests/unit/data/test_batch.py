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

from tree.data.batch import gather_isolated


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
