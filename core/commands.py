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
CODEPAGE_MAP = {
    0: "cp437",
    2: "cp850",
    3: "cp860",
    4: "cp863",
    5: "cp865",
    16: "cp1252",
    17: "cp866",
    18: "cp852",
    19: "cp858",
}
