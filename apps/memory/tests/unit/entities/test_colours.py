"""Unit tests for the brand colour palette enum."""

import re

import pytest

from tree.entities.colours import Colours

_HEX = re.compile(r"^#[0-9a-f]{6}$")


def test_every_colour_is_a_lowercase_six_digit_hex() -> None:
    # Act / Assert
    for colour in Colours:
        assert _HEX.match(colour.value), colour


def test_strenum_member_is_its_hex_string() -> None:
    # Assert: a StrEnum member equals (and can be used as) its hex string.
    assert Colours.BLUE_LEVEL_3 == "#0060b1"
    assert f"color:{Colours.BROWN_LEVEL_3}" == "color:#834622"


def test_palette_has_four_levels_for_each_hue() -> None:
    # Arrange
    hues = ("BLACK", "BROWN", "ORANGE", "BLUE", "YELLOW", "GREEN")

    # Act / Assert: exactly level_1..level_4 per hue, 16 members total.
    for hue in hues:
        levels = sorted(c.name for c in Colours if c.name.startswith(hue))
        assert levels == [f"{hue}_LEVEL_{i}" for i in range(1, 5)]
    assert len(Colours) == len(hues) * 4


@pytest.mark.parametrize(
    ("colour", "k"),
    [
        (Colours.BLACK_LEVEL_1, 25),
        (Colours.BLACK_LEVEL_2, 50),
        (Colours.BLACK_LEVEL_3, 75),
        (Colours.BLACK_LEVEL_4, 100),
    ],
)
def test_black_ramp_matches_cmyk_k_conversion(colour: Colours, k: int) -> None:
    # Black is a CMYK grey ramp: each channel = round(255 * (1 - K / 100)).
    # Arrange
    channel = round(255 * (1 - k / 100))
    expected = f"#{channel:02x}{channel:02x}{channel:02x}"

    # Act / Assert
    assert colour.value == expected
