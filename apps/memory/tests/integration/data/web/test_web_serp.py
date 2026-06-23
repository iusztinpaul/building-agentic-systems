"""Live integration tests for ``tree.data.web.web_serp.search``.

These tests hit the real Bright Data SERP API. They are gated on
``BRIGHTDATA_API_KEY`` and ``BRIGHTDATA_SERP_ZONE`` being set to non-placeholder
values; without them the whole module is skipped so CI / local runs without
secrets stay green.

Each test runs a small, stable query (low cost, deterministic shape) and asserts
on shape rather than exact content — SERP results drift over time.
"""

from __future__ import annotations

import os

import pytest

from tree.data.web.web_serp import search

_PLACEHOLDER_VALUES = {"", "your-brightdata-serp-zone", "your-brightdata-api-key"}


def _is_real(value: str | None) -> bool:
    """Return True if ``value`` is non-empty and not the .env.example placeholder."""

    return bool(value) and value not in _PLACEHOLDER_VALUES


_API_KEY = os.environ.get("BRIGHTDATA_API_KEY")
_SERP_ZONE = os.environ.get("BRIGHTDATA_SERP_ZONE")

# ``slow`: these hit the live Bright Data SERP API and assert on drifting SERP
# content, so they flake on "SERP weather". Mark the whole module slow so the
# deterministic inner-loop gate (``make memory-integration-tests`` → ``-m "not
# slow"``) is not bound to live SERP; they still run in the full
# ``-integration-tests-all`` / ``-slow`` gates (and skip without real creds).
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not (_is_real(_API_KEY) and _is_real(_SERP_ZONE)),
        reason=(
            "BRIGHTDATA_API_KEY / BRIGHTDATA_SERP_ZONE not configured "
            "(or set to placeholder)"
        ),
    ),
]


class TestLiveSerpSearch:
    """Hits the real SERP API. Each test issues 1 SERP credit (or up to 2 for
    pagination) — keep the suite small."""

    async def test_returns_results_with_titles_and_urls(self) -> None:
        """Stable query returns ≥1 result; title + URL non-empty; ≥1 snippet."""

        results = await search("openai gpt-4", engine="google", num_results=5)

        assert len(results) >= 1
        for r in results:
            assert r.title.strip(), f"empty title at rank {r.rank}: {r}"
            assert r.url.startswith("http"), f"non-http url at rank {r.rank}: {r.url}"

        # At least one result should carry a snippet (description) — Bright
        # Data sometimes drops the description for thin pages, but ≥1 is a
        # reasonable lower bound on a 5-result page.
        assert any(r.snippet.strip() for r in results), (
            "expected at least one result with a non-empty snippet"
        )

    async def test_empty_query_returns_empty_list(self) -> None:
        """A nonsense query returns ``[]`` — never raises.

        Uses a quoted phrase of random alphanumerics to suppress Google's
        "did you mean" near-match expansion. Without quotes, Google's HTML
        SERP surfaces tangentially-related content (videos, "missing X"
        suggestions) which the parser would correctly extract as organic
        results — defeating the empty-result contract.
        """

        results = await search(
            '"qzxcvbnm1234567890zxcvbnmqwerty asdfgh poiuyt"',
            engine="google",
            num_results=5,
        )

        assert results == []

    async def test_common_query_returns_at_least_one_organic_result(self) -> None:
        """Regression for the search_web empty-results bug.

        Prior to the #012 fix, ``search("pizza")`` against the configured
        Bright Data SERP zone returned ``[]`` despite the same zone+key+URL
        succeeding via direct ``curl``. This test asserts the fix sticks: a
        common, stable query must return >= 1 organic result.

        The query is intentionally generic so SERP drift over time does not
        flake the test. ``pizza`` matches the user's working curl exactly, so
        if it returns ``[]`` we have a real regression — not a deflated SERP.
        """

        results = await search("pizza", engine="google", num_results=10)

        assert len(results) >= 1, (
            "Expected >= 1 organic result for the stable query 'pizza'; got 0. "
            "This is the regression the fix in #012 must close — the user's "
            "curl with the same zone+key returns a populated SERP."
        )
        # Shape assertions stay loose: SERP content drifts.
        first = results[0]
        assert first.title.strip(), f"empty title at rank {first.rank}: {first}"
        assert first.url.startswith("http"), (
            f"non-http url at rank {first.rank}: {first.url}"
        )
