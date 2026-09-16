"""Tests for render.scroll_policy: the pure smart-auto-scroll predicate.

Kept independent of Tkinter -- the widget layer only needs to read
`canvas.yview()` into a (top, bottom) tuple and hand it to
`is_scrolled_to_bottom`, then feed that boolean to
`should_autoscroll_on_new_content`.
"""

from render.scroll_policy import is_scrolled_to_bottom, should_autoscroll_on_new_content


def test_is_scrolled_to_bottom_true_when_fully_visible():
    assert is_scrolled_to_bottom((0.0, 1.0)) is True


def test_is_scrolled_to_bottom_true_within_floating_point_tolerance():
    assert is_scrolled_to_bottom((0.4, 0.999)) is True


def test_is_scrolled_to_bottom_false_when_scrolled_up():
    assert is_scrolled_to_bottom((0.0, 0.5)) is False


def test_is_scrolled_to_bottom_false_when_near_bottom_but_not_close_enough():
    assert is_scrolled_to_bottom((0.5, 0.95)) is False


def test_should_autoscroll_only_when_previously_at_bottom():
    assert should_autoscroll_on_new_content(True) is True
    assert should_autoscroll_on_new_content(False) is False
