"""Tests for ``tree.memory.resolution.types._normalize``."""

import pytest

from tree.memory.resolution import _normalize


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Alice", "alice"),
        ("  Alice   Smith ", "alice smith"),
        ("ALICE", "alice"),
        ("Alice  Smith", "alice smith"),
        ("\tAlice\nSmith\t", "alice smith"),
        ("alice smith", "alice smith"),
        ("José", "josé"),
        ("  José   García  ", "josé garcía"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_canonicalizes_whitespace_and_case(raw: str, expected: str) -> None:
    # Arrange / Act
    result = _normalize(raw)

    # Assert
    assert result == expected
