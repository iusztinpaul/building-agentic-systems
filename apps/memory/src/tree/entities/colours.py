"""Tree Memory design palette.

Four hue families (black, brown, orange, blue), each in four levels from
``level_1`` (lightest tint) to ``level_4`` (darkest shade). Hex values are
transcribed from the brand colour sheet:

* **Black** is a CMYK grey ramp — ``K=25/50/75/100`` converted to RGB greys.
* **Brown / orange / blue** come straight from the sheet's R/G/B values.

``Colours`` is a :class:`~enum.StrEnum`, so each member *is* its hex string
(e.g. ``Colours.BLUE_LEVEL_3 == "#0060b1"``) and can be used anywhere a CSS
colour string is expected without ``.value``.
"""

from enum import StrEnum


class Colours(StrEnum):
    """Brand palette: ``<HUE>_LEVEL_<1-4>`` → hex colour string."""

    # Black — CMYK grey ramp: RGB = round(255 * (1 - K / 100)).
    BLACK_LEVEL_1 = "#bfbfbf"  # K=25
    BLACK_LEVEL_2 = "#808080"  # K=50
    BLACK_LEVEL_3 = "#404040"  # K=75
    BLACK_LEVEL_4 = "#000000"  # K=100

    # Brown
    BROWN_LEVEL_1 = "#ecd5b8"  # R=236 G=213 B=184
    BROWN_LEVEL_2 = "#d1a672"  # R=209 G=166 B=114
    BROWN_LEVEL_3 = "#834622"  # R=131 G=70  B=34
    BROWN_LEVEL_4 = "#591f06"  # R=89  G=31  B=6

    # Orange
    ORANGE_LEVEL_1 = "#fee3ac"  # R=254 G=227 B=172
    ORANGE_LEVEL_2 = "#ffb458"  # R=255 G=180 B=88
    ORANGE_LEVEL_3 = "#e37b45"  # R=227 G=123 B=69
    ORANGE_LEVEL_4 = "#cc4e01"  # R=204 G=78  B=1

    # Blue
    BLUE_LEVEL_1 = "#c5dfef"  # R=197 G=223 B=239
    BLUE_LEVEL_2 = "#6ba5d7"  # R=107 G=165 B=215
    BLUE_LEVEL_3 = "#0060b1"  # R=0   G=96  B=177
    BLUE_LEVEL_4 = "#002d8b"  # R=0   G=45  B=139

    # Yellow
    YELLOW_LEVEL_1 = "#fefad5"  # R=254 G=250 B=213
    YELLOW_LEVEL_2 = "#fef180"  # R=254 G=241 B=128
    YELLOW_LEVEL_3 = "#e6cb00"  # R=230 G=203 B=0
    YELLOW_LEVEL_4 = "#cca000"  # R=204 G=160 B=0

    # Green
    GREEN_LEVEL_1 = "#ddf8cd"  # R=221 G=248 B=205
    GREEN_LEVEL_2 = "#c2e373"  # R=194 G=227 B=115
    GREEN_LEVEL_3 = "#80c21d"  # R=128 G=194 B=29
    GREEN_LEVEL_4 = "#0a8902"  # R=10  G=137 B=2
