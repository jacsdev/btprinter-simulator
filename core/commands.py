"""Raw ESC/POS control byte constants used by the parser.

These are the single-byte introducers the parser dispatches on. The
actual command tables (ESC ..., GS ...) are decoded in `core/parser.py`,
keeping the numeric vocabulary in one place for readability.
"""

LF = 0x0A  # line feed: print buffer and feed one line
ESC = 0x1B  # introduces an "ESC x ..." command
GS = 0x1D  # introduces a "GS x ..." command
DLE = 0x10  # introduces a real-time (DLE) command, e.g. status queries
FS = 0x1C  # introduces an "FS x ..." (Kanji-mode and related) command
EOT = 0x04  # end-of-transmission, used in "DLE EOT n" status queries
NUL = 0x00  # string terminator used by the legacy GS k barcode form

# Other C0 control bytes the parser dispatches on directly (see
# `Parser._try_parse_one`). None of these are printable content, so none
# of them may be allowed to fall through to `_parse_text`.
CR = 0x0D  # carriage return: redundant alongside LF, consumed without effect
HT = 0x09  # horizontal tab: no tab-stop table is modeled, consumed as a no-op
FF = 0x0C  # form feed: only meaningful in page mode, which is not implemented
CAN = 0x18  # cancel print buffer: only meaningful in page mode, which is not implemented
SO = 0x0E  # legacy one-line double-width on (dot-matrix heritage command)
SI = 0x0F  # legacy one-line double-width off (dot-matrix heritage command)
ENQ = 0x05  # enquiry: no standalone meaning in this profile
DC2 = 0x12  # device control 2: no standalone meaning in this profile
SYN = 0x16  # synchronous idle: no standalone meaning in this profile

# ESC/POS code table id -> Python codec name, for `ESC t n`.
#
# `ESC t n` never changes how bytes are produced by the sender; it only
# selects which table the printer uses to interpret bytes it already
# received. Byte production (the sender's codec) and byte interpretation
# (this table) are independent, and when they disagree the printer
# produces mojibake on paper -- which this simulator must reproduce
# faithfully rather than silently correct.
#
# The id numbering follows Epson's TM-series ESC/POS numbering, sourced
# from the `receipt-print-hq/escpos-printer-db` project's compiled
# `dist/capabilities.json` (the "TM-T88V" and merged "default" printer
# profiles' `codePages` tables -- both agree on every id below). Each
# entry is included only if Python's stdlib `codecs` module actually
# ships a matching codec (verified with `codecs.lookup`); this is a
# decoding capability table for *this simulator*, independent of
# `implemented_codepages` (see `Parser.__init__`), which instead models
# a specific printer's hardware limitations.
#
# Deliberately NOT mapped, with the reason for each:
#   - 6, 7, 8, 20, 22-26, 66-75, 82, 254, 255: listed as "Unknown" in
#     escpos-printer-db itself -- no printer in that database assigns a
#     real code page to these ids, so there is nothing to decode them
#     as. 255 is explicitly a "no code page selected" sentinel.
#   - 11 (CP851), 12 (CP853), 41 (CP1098), 42 (CP774), 43 (CP772): real,
#     named code pages in escpos-printer-db, but Python's stdlib has no
#     matching codec for any of them.
#   - 30 (TCVN-3-1), 31 (TCVN-3-2): Vietnamese code pages with no
#     Python stdlib codec either.
CODEPAGE_MAP = {
    0: "cp437",
    1: "cp932",
    2: "cp850",
    3: "cp860",
    4: "cp863",
    5: "cp865",
    13: "cp857",
    14: "cp737",
    15: "iso8859_7",
    16: "cp1252",
    17: "cp866",
    18: "cp852",
    19: "cp858",
    21: "cp874",
    32: "cp720",
    33: "cp775",
    34: "cp855",
    35: "cp861",
    36: "cp862",
    37: "cp864",
    38: "cp869",
    39: "iso8859_2",
    40: "iso8859_15",
    44: "cp1125",
    45: "cp1250",
    46: "cp1251",
    47: "cp1253",
    48: "cp1254",
    49: "cp1255",
    50: "cp1256",
    51: "cp1257",
    52: "cp1258",
    # 53 (RK1048/Kazakh): escpos-printer-db gives no `python_encode`
    # hint for this one, but Python's stdlib does ship a matching
    # codec under a different name ("kz1048") -- verified directly via
    # `codecs.lookup("kz1048")` rather than via that project's data.
    53: "kz1048",
}
