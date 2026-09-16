"""Pure scroll-position policy for the receipt viewer's smart auto-scroll.

Tkinter's `Canvas.yview()` returns a `(top_fraction, bottom_fraction)`
pair describing what portion of the scrollable region is currently
visible. This module turns that pair into a yes/no auto-scroll
decision, kept separate from any Tk widget so the policy itself can be
exercised with plain assertions instead of a live GUI session.
"""

from __future__ import annotations

from typing import Tuple

# Tolerance for floating point rounding in Canvas.yview()'s reported
# fractions -- "close enough to the bottom" counts as at the bottom.
_BOTTOM_EPSILON = 0.02


def is_scrolled_to_bottom(view_fraction: Tuple[float, float]) -> bool:
    """Return True when the visible viewport's bottom edge is at (or
    effectively at) the bottom of the scrollable region.

    `view_fraction` is the `(top, bottom)` tuple `Canvas.yview()`
    returns. A fully-visible canvas (nothing to scroll) reports
    `(0.0, 1.0)`, which counts as being at the bottom.
    """
    _, bottom = view_fraction
    return bottom >= 1.0 - _BOTTOM_EPSILON


def should_autoscroll_on_new_content(was_at_bottom_before_update: bool) -> bool:
    """Decide whether newly arrived content should pull the viewport down.

    The rule is intentionally simple and named as its own function
    (rather than inlined at the call site) so it is a single, obvious
    place to test: scroll to the newest content only if the user was
    already at the bottom before the update arrived. A user who
    deliberately scrolled up to inspect an earlier receipt keeps their
    view untouched -- new content must never yank the view away from
    them.
    """
    return was_at_bottom_before_update
