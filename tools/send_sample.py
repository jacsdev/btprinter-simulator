"""Send a sample ESC/POS ticket to a running simulator over TCP.

Run `python main.py --transport tcp --port 9100` in one shell, then this
script in another, to see a sample receipt rendered live:

    python tools/send_sample.py
    python tools/send_sample.py --host 127.0.0.1 --port 9100

The ticket exercises: header text, bold/underline/alignment styling,
two-column-ish spacing via plain spaces, a separator line, a barcode, a
QR code, a feed, and a paper cut.
"""

from __future__ import annotations

import argparse
import socket
import sys


def esc(*b: int) -> bytes:
    return bytes([0x1B, *b])


def gs(*b: int) -> bytes:
    return bytes([0x1D, *b])


def build_sample_ticket() -> bytes:
    out = bytearray()

    out += esc(0x40)  # ESC @ - initialize

    # Header: centered, bold, double-width/height
    out += esc(0x61, 1)  # align center
    out += esc(0x45, 1)  # bold on
    out += gs(0x21, 0x11)  # size: width x2, height x2
    out += "SOFTTUR TRAVEL\n".encode("cp437")
    out += gs(0x21, 0x00)  # size back to normal
    out += esc(0x45, 0)  # bold off
    out += "Passenger Receipt\n".encode("cp437")
    out += esc(0x61, 0)  # align left

    out += b"-" * 32 + b"\x0a"

    # Two "columns" via fixed-width padding (32 chars/line at Font A/58mm)
    out += "Tour date: ".ljust(16).encode("cp437") + b"2026-09-20\n"
    out += "Pax: ".ljust(16).encode("cp437") + b"2 adults\n"
    out += "Ticket #: ".ljust(16).encode("cp437") + b"SFT-000123\n"

    out += esc(0x2D, 1)  # underline on
    out += "Itinerary\n".encode("cp437")
    out += esc(0x2D, 0)  # underline off
    out += "Airport -> Hotel -> City Tour\n".encode("cp437")

    out += b"-" * 32 + b"\x0a"

    # Barcode: CODE128 (length-prefixed form), m=73
    barcode_data = b"SFT000123"
    out += gs(0x68, 80)  # barcode height
    out += gs(0x77, 2)  # barcode module width
    out += gs(0x48, 2)  # HRI below
    out += gs(0x6B, 73, len(barcode_data)) + barcode_data

    out += b"\x0a"

    # QR code: model 2, size 6, EC level M, pointing at a ticket URL
    qr_data = b"https://softtur.example/ticket/SFT-000123"
    out += bytes([0x1D, 0x28, 0x6B, 0x04, 0x00, 0x31, 0x41, 0x32, 0x00])  # model
    out += bytes([0x1D, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x43, 0x06])  # size
    out += bytes([0x1D, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x45, 0x31])  # EC level M
    store_payload = bytes([0x31, 0x50, 0x30]) + qr_data
    plen = len(store_payload)
    out += bytes([0x1D, 0x28, 0x6B, plen & 0xFF, (plen >> 8) & 0xFF]) + store_payload
    out += bytes([0x1D, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x51, 0x30])  # print

    out += esc(0x61, 1)  # center
    out += "Thank you for choosing SoftTur!\n".encode("cp437")
    out += esc(0x61, 0)

    out += esc(0x64, 3)  # feed 3 lines
    out += gs(0x56, 65, 0)  # full cut, no extra feed

    return bytes(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a sample ESC/POS ticket over TCP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    ticket = build_sample_ticket()
    with socket.create_connection((args.host, args.port), timeout=5.0) as sock:
        sock.sendall(ticket)
    print(f"Sent {len(ticket)} bytes to {args.host}:{args.port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
