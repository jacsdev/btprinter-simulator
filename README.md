# 58mm/80mm ESC/POS Thermal Printer Simulator

Renders on screen exactly what a real 58mm (or 80mm) Bluetooth thermal
printer would put on paper, by parsing the same raw ESC/POS byte stream
a mobile app would send it. Slice 1 covered the ESC/POS parser, the
renderer, and a TCP transport adapter that stands in for the real
Bluetooth connection. Slice 2 adds two real Bluetooth transports: a raw
RFCOMM server socket with manual SDP registration, and a fallback that
reads from a Windows "Bluetooth Serial Port (incoming)" COM port.

## Running it

```
pip install -r requirements.txt
python main.py --transport tcp --port 9100 --width 58
```

This opens a Tkinter window and starts listening for raw ESC/POS bytes
on `127.0.0.1:9100`. In another shell, send a sample ticket:

```
python tools/send_sample.py --port 9100
```

The window updates live as bytes arrive. `--width` accepts `58` (384
dots) or `80` (576 dots) at 203 dpi.

Press **s** while the window has focus to save the current receipt to a
PNG file via a save dialog.

## Bluetooth transports (slice 2)

Two adapters behind the same `transport/port.py` interface, chosen with
`--transport`:

```
python main.py --transport rfcomm --width 58
python main.py --transport serial --com COM5 --width 58
```

- **`rfcomm`** (`transport/rfcomm.py`, stdlib only): opens a raw
  `AF_BLUETOOTH`/`BTPROTO_RFCOMM` server socket and publishes an SDP
  record for it via `ctypes` calls into `ws2_32.dll`'s
  `WSASetServiceW`, advertising the well-known Serial Port Profile
  (SPP) UUID `00001101-0000-1000-8000-00805F9B34FB` -- the same UUID
  Android's `createRfcommSocketToServiceRecord` looks up. A plain
  Python `AF_BLUETOOTH` socket is otherwise invisible to SDP discovery.
  The RFCOMM channel is not something you choose: this adapter tries
  `bind()` to channel 0 (`BT_PORT_ANY`) first and, if the OS does not
  actually assign a channel from that (confirmed to be the case on
  this development machine -- see the module docstring), falls back to
  scanning explicit channels 4-30.
- **`serial`** (`transport/serialport.py`, needs `pyserial`): reads
  bytes from a Windows-managed "Bluetooth Serial Port (incoming)" COM
  port. Windows publishes the SDP record for that COM port itself, so
  this path needs no manual SDP registration -- only pairing the phone
  and configuring the incoming COM port in Bluetooth settings.
  `pyserial` is imported lazily, so it's only required if you actually
  start this transport.

## Diagnostics: `--doctor`

```
python main.py --doctor
```

Runs read-only checks and prints one `[PASS]` / `[FAIL]` / `[UNKNOWN]`
line per check, with a concrete remedy on failure: the `bthserv` and
`BTAGService` Windows services, binding a real RFCOMM channel,
registering an SDP record on it, PC discoverability (always
`[UNKNOWN]` with the manual Windows 11 path, since there is no reliable
programmatic check for this), and listing paired Bluetooth devices and
Bluetooth-related COM ports via PowerShell. It never prints `[PASS]`
for a check it did not actually run.

## Manual verification required

SDP visibility to a real Android device, and actual Bluetooth pairing,
cannot be exercised by any unit test -- they need a second, physically
paired device. After `python -m pytest` passes, verify by hand:

1. Pair the phone with this PC (Windows Settings > Bluetooth & devices
   > Add device), or run `python main.py --doctor` and follow its PC
   discoverability instructions first if the PC does not show up.
2. Run `python main.py --transport rfcomm --width 58`. Note the
   "RFCOMM (channel auto-assigned)" window title and the logged
   channel number.
3. From the target Flutter app (`print_bluetooth_thermal`), connect to
   this PC's Bluetooth device and send a print job.
4. Confirm the bytes appear rendered live in the viewer window exactly
   as they would on a real 58mm printer.

If discovery fails, run `python main.py --doctor` again: a `[FAIL]` on
SDP registration will show the real Win32 error code from
`WSASetServiceW`.

## Architecture

```
[ transport ] --bytes--> [ core (parser) ] --render ops--> [ render ]
```

- `transport/` moves raw bytes and knows nothing about ESC/POS:
  `transport/tcp.py` (slice 1), `transport/rfcomm.py` and
  `transport/serialport.py` (slice 2, Bluetooth Classic SPP), all
  behind the same `transport/port.py` interface.
- `core/` (`core/parser.py`, `core/state.py`, `core/commands.py`,
  `core/ops.py`) turns the byte stream into a list of render
  operations. It has **no import of `transport/` or `render/`** — see
  the boundary check below.
- `render/` (`render/raster.py`, `render/viewer.py`) turns render
  operations into a PIL image and displays it live in a Tkinter window.

This mirrors real 58mm printer hardware: in a cheap thermal printer the
Bluetooth module is just an SPP-to-UART bridge, and the print MCU that
interprets ESC/POS commands has no notion that Bluetooth (or, here,
TCP) exists. Keeping that boundary in code means slice 2 only has to
add a new `transport/bluetooth.py` implementing `Port` — the parser and
renderer do not change.

To re-verify the boundary yourself:

```
grep -rnE "^\s*(import|from)\s+(transport|render)\b" core/
grep -rnE "^\s*(import|from)\s+transport\b" render/
```

Both should print nothing.

## Composition root

`main.py` is the only module allowed to import from `transport/`,
`core/` and `render/` at once. It picks the transport adapter from
`--transport`, feeds every received chunk to `core.parser.Parser`, and
hands the resulting ops to the viewer — hopping back onto the Tk main
thread via `Viewer.call_soon()` since the transport delivers bytes on a
background thread.

## Supported ESC/POS commands (slice 1)

LF, ESC @, ESC a, ESC E, ESC -, ESC M, ESC !, ESC t, ESC R, ESC d,
ESC e, ESC 2 / ESC 3, ESC {, ESC V, ESC p (cash drawer event), ESC *
(column bit image), GS !, GS B, GS V (paper cut), GS v 0 (raster bit
image), GS k (barcode, both NUL-terminated and length-prefixed forms),
GS h / GS w / GS f / GS H (barcode configuration), GS ( k (QR code:
model/size/error-correction/store/print), and DLE EOT (status query —
parsed and discarded, never answered).

Barcodes and QR codes are rendered as a labeled placeholder box showing
the symbology/config and the encoded data rather than a scannable
symbol — getting the byte-level parsing right matters more for a
debugging tool than a perfect glyph, and this adds no new dependencies.

Any unrecognized or malformed command is logged (with its offset in the
stream and the raw hex bytes) and skipped without raising. The parser
buffers incomplete commands across `feed()` calls, so a command split
across two socket reads still parses correctly once the rest arrives.

## Out of scope

- Any read/response path. `print_bluetooth_thermal` (the Flutter
  package used by the real app) has no read method — a real printer
  connection is write-only from the app's side. `DLE EOT` status
  queries are parsed only to stay in sync with the byte stream, then
  discarded; nothing is ever written back to a real printer.
- Scannable barcode/QR rendering (see above).
- Non-uniform bold weight rendering: `ESC E` / bold state is tracked
  correctly, but PIL cannot synthesize a bold weight from a single
  regular-weight TTF, so bold text renders at the regular weight unless
  a bold font file is added later.

## Tests

```
python -m pytest
```

Strict TDD was used throughout: every command, style transition,
streaming/split-command case, and the unknown-byte robustness
requirement has a test that was written and observed to fail before
the corresponding implementation was added. The one exception is the
four interdependent ctypes Win32 structs (`GUID`, `SOCKADDR_BTH`,
`CSADDR_INFO`, `WSAQUERYSETW`) in `transport/rfcomm.py`, which were
implemented together after their tests, since they only compile/link
meaningfully as a group.
