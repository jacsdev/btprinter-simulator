"""Tests for character-per-line math, independent of PIL font loading.

Verified metrics (203 dpi): Font A = 12x24 dots -> 32 chars/line at 384
dots (58mm), 48 at 576 (80mm). Font B = 9x17 dots -> 42 chars/line at
384, 64 at 576.
"""

import pytest

from render.raster import FONT_METRICS, chars_per_line


@pytest.mark.parametrize(
    "width_dots,font,expected",
    [
        (384, "A", 32),
        (576, "A", 48),
        (384, "B", 42),
        (576, "B", 64),
    ],
)
def test_chars_per_line_matches_verified_metrics(width_dots, font, expected):
    assert chars_per_line(width_dots, font) == expected


def test_font_metrics_table_has_expected_dot_sizes():
    assert FONT_METRICS["A"] == (12, 24)
    assert FONT_METRICS["B"] == (9, 17)
