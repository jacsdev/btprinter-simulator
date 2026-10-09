# Developer-tool iteration: status responses, RFCOMM diagnosis, one-click run

## Goal

Turn the simulator from "works for the one Flutter app" into a general
development tool: answer real-time status queries so any app/driver that
reads from the printer works, close (or honestly bound) the native RFCOMM
gap, and remove the Python-knowledge barrier with a double-click `.exe`.

## Decisions (this session, 2026-10-09)

1. **Status queries**: v1 responds as a permanently healthy printer
   (online, paper present, cover closed, no error). No fault-injection
   in v1.
2. **Distribution**: self-contained Windows `.exe` via PyInstaller
   (windowed, no console), not `uv tool`/pipx.
3. **RFCOMM**: investigate + harden diagnostics now; end-to-end
   verification requires the user's physical Android phone at the end.

## Non-goals

- Fault simulation (out-of-paper / cover-open / overheat) — deferred.
- Scannable barcode/QR rendering — still placeholders.
- Cross-platform (Linux/macOS) transports — still Windows-first.
- Replacing the working `serial` path; it remains the recommended one.

## Architecture facts that shape the work

- `transport/port.py` already exposes `write(bytes)` and all three
  adapters (`tcp`, `serialport`, `rfcomm`) implement it correctly; it is
  simply never called. No interface change needed.
- `core/parser.py:_parse_dle` consumes `DLE EOT n` and returns `[]`,
  discarding it. The response must NOT be a render op: it is a side
  effect. Follow the existing `take_diagnostics()` polling pattern.
- `core/` must never import `transport/`; `main.py` (composition root)
  is the only place that may route parser-produced response bytes to
  `port.write()`.

## Tasks

- [x] **T1 — Status response channel (core/parser.py)** — collect response
  bytes for status queries in the parser; add `take_status_responses()`.
  TDD: `DLE EOT n` populates one response; plain text/other commands do not.
- [x] **T2 — DLE EOT n response bytes (core/parser.py)** — `_parse_dle`
  emits the healthy status byte(s) for n=1 (printer), n=2 (offline),
  n=3 (error), n=4 (paper roll). Exact byte values referenced from the
  ESC/POS spec, asserted in tests.
- [x] **T3 — Route responses to transport (main.py)** — after each
  `feed()`, drain `take_status_responses()` and call `port.write(...)`.
  TDD with a fake `Port` capturing writes.

  **Done (2026-10-09, delegated to gentle-ai-worker, verified inline).**
  Healthy byte is 0x12 for all four n (Epson TM "all clear": bits 1/4
  set). 488 tests pass (was 480). Files: `core/parser.py`, `main.py`,
  `tests/test_status_responses.py` (new), `tests/test_main.py`.
  Boundary grep clean.
- [ ] **T4 — GS a (ASB) enable response (optional)** — `GS a n` enable
  writes the initial 4-byte ASB status. Flagged; pull in only after T1-T3.
- [x] **T5 — RFCOMM static investigation (transport/)** — re-read
  `rfcomm.py` + `sdp_encoding.py`; test the two hypotheses (SDP record
  shape vs. Store-Python AppContainer); record concrete findings.

  **Done (2026-10-09, delegated to gentle-ai-explore).** Findings:
  blob record is byte-correct (H1 not supported); `--doctor` only tests
  the simple path while auto uses blob first; `_accept_loop` has two
  silent exits; AppContainer check is a substring heuristic; blob dereg
  handle==0 hole. Root cause still needs hardware A/B.
- [x] **T6 — Harden --doctor / --inspect-sdp** — sharper SDP record
  decode (verify ProtocolDescriptorList and that the advertised channel
  matches the bound channel); more precise Store/AppContainer detection.

  **Done.** `check_sdp_registration` now mirrors the runtime auto path
  (blob first, simple fallback, reports which); AppContainer check now
  uses real `GetTokenInformation(TokenIsAppContainer)` via ctypes.
- [x] **T7 — Fix any confirmed RFCOMM defect** — only if T5 finds a
  concrete, testable bug (e.g. blob record missing a usable PDL). TDD.

  **Done.** Logged the two silent `_accept_loop` exits; auto blob->simple
  fallback now logs WARNING; blob dereg tracked via a `_blob_registered`
  boolean (no longer dependent on handle truthiness).

  **T6/T7 evidence (2026-10-09, delegated to gentle-ai-worker, verified
  inline):** 496 tests pass (was 488). Files: `transport/rfcomm.py`,
  `transport/diagnostics.py`, `tests/test_rfcomm.py`,
  `tests/test_diagnostics.py`. `core/` and `render/` untouched.
- [ ] **T8 — RFCOMM verification checklist (README)** — step-by-step
  phone-verification runbook so the user can close the loop with hardware.
- [ ] **T9 — PyInstaller windowed .exe** — build script/spec producing a
  single double-click `.exe`; runtime logs to a file; verify build.
- [ ] **T10 — COM auto-detection (main.py)** — when `--transport serial`
  has no `--com`, auto-list/auto-pick the incoming BT COM port (reuse the
  PowerShell listing already in `transport/diagnostics.py`), so the `.exe`
  works on double-click. TDD for the detection logic.

## Definition of done

- `python -m pytest -q` green before and after every task.
- `core/` imports neither `transport/` nor `render/` (boundary grep clean).
- No test fakes a result that was not actually verified.
- README updated for any user-visible change (status responses, `.exe`,
  auto-detection).

## Commit policy (project rule, non-negotiable)

Commits are authored solely by the human developer. Work here is
implemented and tested; the human runs `git commit` (or explicitly asks
for it). No commit, and no attribution trailer, is added by the agent.
