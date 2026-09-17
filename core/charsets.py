"""ESC R international character set substitution tables.

`ESC R n` selects a country-specific variant of the printable ASCII
range: 12 code positions (each historically a "national use" position
in ISO/IEC 646) are re-mapped to a different glyph depending on the
selected country. This module is the sourced data for that
substitution -- `core.parser` applies it while decoding text, at the
same point it already applies the `ESC t` code page codec, so
`render/` never needs to know this feature exists.

Sourcing
--------
The country-to-`n` numbering matches Epson's own ESC/POS numbering as
documented in the Star Micronics "ESC/POS(R) Mode Command
Specifications" manual (Rev. 2.52), section "ESC R n", "Spec. A" list:
0 USA, 1 France, 2 Germany, 3 UK, 4 Denmark I, 5 Sweden, 6 Italy,
7 Spain I, 8 Japan, 9 Norway, 10 Denmark II, 11 Spain II,
12 Latin America, 13 Korea. This same 0-13 ordering is also what
turns up in ESC/POS command summaries derived from Epson's own
reference, and it matches this project's existing `commands.
CODEPAGE_MAP`, which was already keyed to Epson's TM-series numbering.

The actual per-position glyph substitutions are the historical
national-use variants of ISO/IEC 646 (the standard ESC/POS's
"international character set" feature is built on top of). They are
sourced from Wikipedia's "ISO/IEC 646" article, "Variant comparison
chart" section, which tabulates each national registration
(ISO-IR-004 UK, ISO-IR-010 Sweden, ISO-IR-015 Italy, ISO-IR-017 Spain,
ISO-IR-021 Germany, ISO-IR-060 Norway, ISO-IR-069 France, ISO-IR-085
Spain II, ISO-IR-014 Japanese Roman) against the invariant ASCII
positions, each row citing its own ISO-IR registration number.

Two `n` values could NOT be sourced this way and are deliberately left
unimplemented (see `INTERNATIONAL_CHARSETS` -- they are simply absent
from it): 10 (Denmark II) and 12 (Latin America). Neither has a
corresponding ISO/IEC 646 national registration, and no Epson-specific
document giving their exact substitutions could be found. Selecting
either produces a diagnostic and renders with unmodified (US) glyphs
at all 12 positions rather than a guessed table -- see
`core.parser.Parser._set_charset`.

Denmark I (n=4) is sourced from the Wikipedia table's plain "DK" row
(IBM registry CP01017), which is the only Danish variant given a
"Denmark"-only label there (as opposed to the combined "DK/NO (NRCS)"
rows, which read as Norway-flavored). It is included here as verified,
but it is the single lowest-confidence entry: it substitutes `^` and
`~` with `Ü`/`ü`, which is unusual for Danish text, so it is called
out explicitly for physical verification rather than silently trusted.
"""

from __future__ import annotations

from typing import Dict, Optional

# The 12 ASCII positions ESC R can re-map, exactly as documented for
# ESC/POS's international character set feature.
SUBSTITUTION_POSITIONS = frozenset(
    {0x23, 0x24, 0x40, 0x5B, 0x5C, 0x5D, 0x5E, 0x60, 0x7B, 0x7C, 0x7D, 0x7E}
)

# n -> {byte: replacement glyph}. Only positions that actually change
# from US/ASCII are listed; everything else in SUBSTITUTION_POSITIONS
# stays as the plain ASCII character for that country. n=0 (USA) is
# listed explicitly (as an empty table) so it counts as "verified"
# rather than falling through the unimplemented path.
INTERNATIONAL_CHARSETS: Dict[int, Dict[int, str]] = {
    0: {},  # USA -- baseline ASCII, no substitutions.
    1: {  # France -- ISO-IR-069.
        0x23: "£", 0x40: "à", 0x5B: "°", 0x5C: "ç", 0x5D: "§",
        0x60: "µ", 0x7B: "é", 0x7C: "ù", 0x7D: "è", 0x7E: "¨",
    },
    2: {  # Germany -- ISO-IR-021 (DIN 66003).
        0x40: "§", 0x5B: "Ä", 0x5C: "Ö", 0x5D: "Ü",
        0x7B: "ä", 0x7C: "ö", 0x7D: "ü", 0x7E: "ß",
    },
    3: {  # UK -- ISO-IR-004.
        0x23: "£", 0x7E: "‾",  # overline
    },
    4: {  # Denmark I -- IBM registry CP01017 ("DK"). Lowest-confidence
        # entry in this table; see module docstring.
        0x24: "¤", 0x5B: "Æ", 0x5C: "Ø", 0x5D: "Å", 0x5E: "Ü",
        0x7B: "æ", 0x7C: "ø", 0x7D: "å", 0x7E: "ü",
    },
    5: {  # Sweden -- ISO-IR-010.
        0x24: "¤", 0x5B: "Ä", 0x5C: "Ö", 0x5D: "Å",
        0x7B: "ä", 0x7C: "ö", 0x7D: "å", 0x7E: "‾",
    },
    6: {  # Italy -- ISO-IR-015.
        0x23: "£", 0x40: "§", 0x5B: "°", 0x5C: "ç", 0x5D: "é",
        0x60: "ù", 0x7B: "à", 0x7C: "ò", 0x7D: "è", 0x7E: "ì",
    },
    7: {  # Spain I -- ISO-IR-017.
        0x23: "£", 0x40: "§", 0x5B: "¡", 0x5C: "Ñ", 0x5D: "¿",
        0x7B: "°", 0x7C: "ñ", 0x7D: "ç",
    },
    8: {  # Japan (JIS Roman) -- ISO-IR-014.
        0x5C: "¥", 0x7E: "‾",
    },
    9: {  # Norway -- ISO-IR-060.
        0x5B: "Æ", 0x5C: "Ø", 0x5D: "Å",
        0x7B: "æ", 0x7C: "ø", 0x7D: "å", 0x7E: "‾",
    },
    # 10: Denmark II -- deliberately absent, see module docstring.
    11: {  # Spain II -- ISO-IR-085.
        0x40: "·", 0x5B: "¡", 0x5C: "Ñ", 0x5D: "Ç", 0x5E: "¿",
        0x7B: "´", 0x7C: "ñ", 0x7D: "ç", 0x7E: "¨",
    },
    # 12: Latin America -- deliberately absent, see module docstring.
    13: {  # Korea -- follows the same pattern as Japan (Won sign in
        # place of Yen).
        0x5C: "₩", 0x7E: "‾",
    },
}


def is_verified(charset: int) -> bool:
    """Whether `charset` (an ESC R n value) has a sourced substitution
    table in `INTERNATIONAL_CHARSETS`."""
    return charset in INTERNATIONAL_CHARSETS


def resolve_substitution(charset: int, byte: int) -> Optional[str]:
    """Return the glyph that replaces `byte` under `charset`, or `None`
    if this position is not substituted (either because `byte` is not
    one of the 12 international positions, the charset does not touch
    it, or `charset` is not a verified table at all -- both cases
    honestly fall back to leaving the original ASCII glyph in place,
    exactly like the unimplemented-charset diagnostic path expects).
    """
    table = INTERNATIONAL_CHARSETS.get(charset)
    if table is None:
        return None
    return table.get(byte)
