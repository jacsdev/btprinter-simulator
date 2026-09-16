"""Tests for render.raster.ruler_x_position: the pixel x-position of the
32/48-column guide, for both supported paper widths and both fonts.
"""

import pytest

from render.raster import ruler_x_position


@pytest.mark.parametrize(
    "width_dots,font,expected_x",
    [
        (384, "A", 384),  # 58mm/Font A: 32 cols * 12 dots/col = 384
        (576, "A", 576),  # 80mm/Font A: 48 cols * 12 dots/col = 576
        (384, "B", 378),  # 58mm/Font B: 42 cols * 9 dots/col = 378
        (576, "B", 576),  # 80mm/Font B: 64 cols * 9 dots/col = 576
    ],
)
def test_ruler_x_position_matches_verified_column_metrics(width_dots, font, expected_x):
    assert ruler_x_position(width_dots, font) == expected_x
