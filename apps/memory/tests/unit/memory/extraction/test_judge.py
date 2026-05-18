"""Unit tests for the contradiction judge (#032).

Exercises both branches (contradiction / no contradiction) via a
mocked :class:`BaseLLM` so the test doesn't need a Gemini API key
or a live network connection.
"""

from __future__ import annotations

from typing import Any

import pytest

from tree.memory.extraction.judge import judge_contradiction
from tree.models.base import BaseLLM


class _StubLLM(BaseLLM):
    """LLM stub that always returns a canned JSON payload."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.prompts: list[str] = []
        self.systems: list[str | None] = []

    async def generate_json(
        self, prompt: str, *, system: str | None = None
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.systems.append(system)
        return self.payload


class _FailingLLM(BaseLLM):
    """LLM stub that raises on every call (used for the safe-default branch)."""

    async def generate_json(
        self, prompt: str, *, system: str | None = None
    ) -> dict[str, Any]:
        raise RuntimeError("simulated LLM outage")


class TestJudgeContradictionBranches:
    async def test_contradiction_branch_returns_true_and_confidence(self) -> None:
        # Arrange
        llm = _StubLLM(
            {
                "is_contradiction": True,
                "confidence": 0.92,
                "reasoning": "Opposing UI preferences.",
            }
        )

        # Act
        is_contradiction, confidence = await judge_contradiction(
            llm=llm,
            new_statement="prefers light mode",
            old_statement="prefers dark mode",
        )

        # Assert
        assert is_contradiction is True
        assert confidence == pytest.approx(0.92)
        # The judge call carries both statements in the prompt
        assert "prefers light mode" in llm.prompts[0]
        assert "prefers dark mode" in llm.prompts[0]

    async def test_paraphrase_branch_returns_false_and_low_confidence(self) -> None:
        llm = _StubLLM(
            {
                "is_contradiction": False,
                "confidence": 0.10,
                "reasoning": "Same preference paraphrased.",
            }
        )

        is_contradiction, confidence = await judge_contradiction(
            llm=llm,
            new_statement="really likes python",
            old_statement="prefers python",
        )

        assert is_contradiction is False
        assert confidence == pytest.approx(0.10)


class TestJudgeContradictionDefensiveParsing:
    """Malformed LLM responses degrade safely to (False, 0.0)."""

    async def test_non_dict_response_defaults_to_no_contradiction(self) -> None:
        llm = _StubLLM("clearly not a dict")  # type: ignore[arg-type]

        result = await judge_contradiction(
            llm=llm,
            new_statement="a",
            old_statement="b",
        )

        # Non-JSON string is normalised; if it's not parseable we get
        # the safe default.
        assert result == (False, 0.0)

    async def test_missing_keys_defaults(self) -> None:
        llm = _StubLLM({})

        result = await judge_contradiction(
            llm=llm,
            new_statement="a",
            old_statement="b",
        )

        assert result == (False, 0.0)

    async def test_confidence_clamped_above_one(self) -> None:
        llm = _StubLLM({"is_contradiction": True, "confidence": 1.5})

        is_contradiction, confidence = await judge_contradiction(
            llm=llm,
            new_statement="a",
            old_statement="b",
        )

        assert is_contradiction is True
        assert confidence == 1.0

    async def test_confidence_clamped_below_zero(self) -> None:
        llm = _StubLLM({"is_contradiction": True, "confidence": -0.5})

        is_contradiction, confidence = await judge_contradiction(
            llm=llm,
            new_statement="a",
            old_statement="b",
        )

        assert is_contradiction is True
        assert confidence == 0.0

    async def test_llm_exception_falls_back_to_no_contradiction(self) -> None:
        # When the LLM call itself raises (timeout / network error /
        # API outage), the judge must NEVER write a spurious
        # ``superseded_by`` edge - the safe default is "not a
        # contradiction".
        result = await judge_contradiction(
            llm=_FailingLLM(),
            new_statement="prefers dark mode",
            old_statement="prefers light mode",
        )

        assert result == (False, 0.0)
