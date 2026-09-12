"""The per-cluster LLM summary: prompt, one call, and the fail-open fallback.

ADR-007 §4. Three claims are worth pinning here, and the LLM is a fake in all of
them (the model is an external boundary; what we own is the prompt and the
contract):

1. the prompt asks for exactly what :class:`ClusterSummary` validates — a
   drifting prompt shows up as retries in production, never as a red test;
2. one cluster is ONE round trip, and a model that ignores the bounds produces
   a ``ValidationError`` (which the task retries) rather than a legend entry
   that wraps over the map;
3. the fallback is built WITHOUT validation, because ``keywords=[]`` is exactly
   what the contract refuses from a model.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from tree.memory.clustering.summaries import (
    SUMMARY_PROMPT_VERSION,
    build_summary_prompt,
    fallback_summary,
    summarise_cluster,
)
from tree.memory.clustering.types import ClusterSummary
from tree.models.base import BaseLLM

_VALID_RESPONSE: dict[str, Any] = {
    "label": "Agent memory design",
    "summary": "How agents store and retrieve what they learn.",
    "keywords": ["memory", "agents", "rag"],
}


class _FakeLLM(BaseLLM):
    """Records every call and returns one canned JSON payload."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[tuple[str, str | None]] = []

    async def generate_json(
        self, prompt: str, *, system: str | None = None
    ) -> dict[str, Any]:
        self.calls.append((prompt, system))
        return self.response


class TestBuildSummaryPrompt:
    def test_numbers_every_sample(self) -> None:
        prompt = build_summary_prompt(["alpha", "beta"])

        # Numbered so the model reads N pieces of evidence, not one run-on doc.
        assert "1. alpha" in prompt
        assert "2. beta" in prompt

    def test_states_how_many_chunks_the_cluster_contributed(self) -> None:
        prompt = build_summary_prompt(["alpha", "beta"])

        assert "2 text chunks" in prompt

    @pytest.mark.parametrize(
        "fragment", ["label", "summary", "keywords", "6 words", "100 words", "3–5"]
    )
    def test_asks_for_the_contract_the_model_is_validated_against(
        self, fragment: str
    ) -> None:
        """Every bound ``ClusterSummary`` enforces must be IN the prompt.

        A prompt that omits "at most 6 words" turns a validator into a retry
        loop that burns billable calls before falling back to ``Cluster {id}``.
        """

        assert fragment in build_summary_prompt(["alpha", "beta"])

    def test_truncates_a_long_sample_to_600_characters(self) -> None:
        long_sample = "x" * 2000

        prompt = build_summary_prompt([long_sample])

        # One outlier chunk must not crowd the other 19 out of the prompt.
        assert "x" * 600 in prompt
        assert "x" * 601 not in prompt

    def test_is_deterministic_for_the_same_samples(self) -> None:
        """The task's INPUTS cache is keyed on the samples: same in, same out."""

        assert build_summary_prompt(["a", "b"]) == build_summary_prompt(["a", "b"])

    def test_prompt_version_is_declared(self) -> None:
        """A prompt edit without a version bump serves 90-day-old labels."""

        assert SUMMARY_PROMPT_VERSION == "v1"


class TestSummariseCluster:
    async def test_returns_the_validated_summary(self) -> None:
        llm = _FakeLLM(_VALID_RESPONSE)

        summary = await summarise_cluster(llm, ["alpha", "beta"])

        assert summary == ClusterSummary(**_VALID_RESPONSE)

    async def test_calls_the_model_exactly_once_with_the_system_prompt(self) -> None:
        llm = _FakeLLM(_VALID_RESPONSE)

        await summarise_cluster(llm, ["alpha", "beta"])

        # One cluster is ONE round trip — the flow fans out over clusters, not
        # over samples.
        assert len(llm.calls) == 1
        prompt, system = llm.calls[0]
        assert "1. alpha" in prompt
        assert system is not None and "topic labeller" in system

    async def test_a_chatty_label_raises_so_the_task_retries(self) -> None:
        llm = _FakeLLM(
            {**_VALID_RESPONSE, "label": "one two three four five six seven"}
        )

        # Deliberately NOT caught here: the task's 2 retries get another
        # sample of the model before the run falls back to "Cluster {id}".
        with pytest.raises(ValidationError):
            await summarise_cluster(llm, ["alpha"])

    async def test_too_few_keywords_raises(self) -> None:
        llm = _FakeLLM({**_VALID_RESPONSE, "keywords": ["memory"]})

        with pytest.raises(ValidationError):
            await summarise_cluster(llm, ["alpha"])


class TestFallbackSummary:
    def test_names_the_cluster_and_carries_no_evidence(self) -> None:
        summary = fallback_summary(4)

        assert summary.label == "Cluster 4"
        assert summary.summary == ""
        assert summary.keywords == []

    def test_is_built_without_validation(self) -> None:
        """The fallback's own shape is what the contract REFUSES from a model.

        If it ever validated, the bounds would have to be widened to admit an
        empty keyword list — and then a lazy model would pass too.
        """

        with pytest.raises(ValidationError):
            ClusterSummary(label="Cluster 4", summary="", keywords=[])

        assert isinstance(fallback_summary(4), ClusterSummary)
