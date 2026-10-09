"""Diagnostics backing `python main.py --doctor`.

Every check here either performs the real operation it claims to
perform, or reports UNKNOWN with the reason -- never a fabricated PASS.
`subprocess.run` is accepted as a parameter (`runner`) purely so tests
can inject a fake instead of actually shelling out; production callers
never need to pass it.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

from transport.rfcomm import (
    SdpRegistrationError,
    _bind_available_channel,
    _default_rfcomm_socket,
    deregister_sdp_service,
    deregister_sdp_service_blob,
    register_sdp_service,
    register_sdp_service_blob,
)

_SPP_UUID = "00001101-0000-1000-8000-00805F9B34FB"
_SubprocessRunner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class CheckResult:
    status: str  # "PASS", "FAIL", "WARNING", or "UNKNOWN"
    message: str


def check_windows_service(
    service_name: str, runner: _SubprocessRunner = subprocess.run
) -> CheckResult:
    """Check whether a Windows service is Running via `sc query`."""
    try:
        completed = runner(
            ["sc", "query", service_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        return CheckResult("UNKNOWN", f"could not run 'sc query {service_name}': {exc}")
    output = completed.stdout or ""
    if "RUNNING" in output.upper():
        return CheckResult("PASS", f"{service_name} is Running")
    return CheckResult(
        "FAIL",
        f"{service_name} is not Running (or not installed). "
        f"Remedy: run 'sc start {service_name}' or check it in services.msc.",
    )


def check_rfcomm_socket_bind() -> CheckResult:
    """Try to create+bind an AF_BLUETOOTH/BTPROTO_RFCOMM socket to a real
    channel, via the same channel-selection path RfcommPort uses (bind
    to port 0 first, falling back to scanning explicit channels -- see
    transport/rfcomm.py's module docstring for why port 0 alone is not
    enough on this machine's Bluetooth stack)."""
    try:
        probe, channel = _bind_available_channel(_default_rfcomm_socket)
    except OSError as exc:
        return CheckResult(
            "FAIL",
            f"could not create an AF_BLUETOOTH/BTPROTO_RFCOMM socket: {exc}. "
            "Remedy: confirm a Bluetooth radio is present and its driver is installed.",
        )
    except RuntimeError as exc:
        return CheckResult(
            "FAIL",
            f"could not bind any RFCOMM channel: {exc}. "
            "Remedy: confirm Bluetooth is turned on and no other process holds every RFCOMM channel.",
        )
    try:
        return CheckResult("PASS", f"bound an RFCOMM socket on channel {channel}")
    finally:
        probe.close()


def check_sdp_registration() -> CheckResult:
    """Register and immediately deregister an SDP record via the same
    code path RfcommPort uses in production (--sdp-mode auto): try the
    hand-built blob record first, then fall back to the simple record,
    on a throwaway channel."""
    try:
        probe, channel = _bind_available_channel(_default_rfcomm_socket)
    except OSError as exc:
        return CheckResult("UNKNOWN", f"could not create a probe socket: {exc}")
    except RuntimeError as exc:
        return CheckResult("UNKNOWN", f"could not bind a probe channel to test SDP registration: {exc}")
    service_name = "58mm Thermal Printer Simulator (doctor probe)"
    try:
        record_kind = None
        blob_handle = None
        try:
            blob_handle = register_sdp_service_blob(channel, service_name)
            record_kind = "blob"
        except SdpRegistrationError:
            try:
                register_sdp_service(channel, service_name)
                record_kind = "simple"
            except SdpRegistrationError as exc:
                return CheckResult(
                    "FAIL",
                    f"WSASetServiceW failed with Win32 error {exc.win32_error_code}. "
                    "Remedy: confirm the Bluetooth Support Service is running and retry as Administrator.",
                )
        try:
            if record_kind == "blob":
                deregister_sdp_service_blob(blob_handle)
            else:
                deregister_sdp_service(channel, service_name)
        except SdpRegistrationError:
            pass  # best-effort cleanup of the probe registration
        record_label = "blob record" if record_kind == "blob" else "simple record"
        return CheckResult(
            "PASS",
            f"published SDP record for SPP UUID {_SPP_UUID} on channel {channel} ({record_label})",
        )
    finally:
        probe.close()


def check_discoverability() -> CheckResult:
    """Windows exposes no reliable programmatic API to confirm the PC is
    discoverable/accepting incoming connections, so this is always
    UNKNOWN with the manual path -- never a guessed PASS/FAIL."""
    return CheckResult(
        "UNKNOWN",
        "Windows has no reliable programmatic check for this. Verify manually on "
        "Windows 11: Settings > Bluetooth & devices > Devices > 'More Bluetooth "
        "options' (or run 'start ms-settings:bluetooth' -- confirmed on this "
        "machine; there is no bluetoothsettings.exe on Windows 11) > Options tab "
        "> enable 'Allow Bluetooth devices to find this PC'.",
    )


def list_paired_bluetooth_devices(runner: _SubprocessRunner = subprocess.run) -> CheckResult:
    try:
        completed = runner(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-PnpDevice -Class Bluetooth | Format-Table -AutoSize | Out-String -Width 200",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return CheckResult("UNKNOWN", f"could not run Get-PnpDevice: {exc}")
    if completed.returncode != 0:
        return CheckResult(
            "UNKNOWN",
            f"Get-PnpDevice returned exit code {completed.returncode}: {completed.stderr.strip()}",
        )
    return CheckResult("PASS", completed.stdout.strip() or "no Bluetooth devices found")


def check_interpreter_restriction() -> CheckResult:
    """Check the real process token's `TokenIsAppContainer` flag via
    `ctypes` instead of guessing from the interpreter install path.

    This is deliberately a WARNING, never a FAIL: an AppContainer token
    is a possibility, not a confirmed root cause. Store Python builds
    run inside an AppContainer sandbox that MAY restrict inbound
    Bluetooth connections even when SDP registration and RFCOMM
    bind/listen both succeed -- which would match this simulator's real
    symptom (a paired phone's RFCOMM connect attempt never reaching the
    accept loop, with nothing logged). Retrying with a python.org
    installation (not a Microsoft Store one) is a valid diagnostic step
    to rule this in or out; this check cannot do that itself.
    """
    if sys.platform != "win32":
        return CheckResult("UNKNOWN", "AppContainer token check is Windows-only")
    try:
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        token = ctypes.c_void_p()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
        ):
            raise ctypes.WinError()
    except Exception as exc:
        return CheckResult("UNKNOWN", f"could not open the process token: {exc}")
    try:
        flag = ctypes.c_ulong(0)
        returned = ctypes.c_ulong(0)
        if not advapi32.GetTokenInformation(
            token, 29, ctypes.byref(flag), ctypes.sizeof(flag), ctypes.byref(returned)
        ):
            raise ctypes.WinError()
    except Exception as exc:
        return CheckResult("UNKNOWN", f"could not read the AppContainer token flag: {exc}")
    if flag.value != 0:
        return CheckResult(
            "WARNING",
            "this process is running under an AppContainer token, which may restrict "
            "inbound Bluetooth connections. This is a possibility, not a confirmed "
            "cause of the RFCOMM connection failure: retrying with a python.org "
            "(non-Store) Python installation is a valid diagnostic step to rule "
            "this in or out.",
        )
    return CheckResult("PASS", "not AppContainer-restricted")


def list_bluetooth_com_ports(runner: _SubprocessRunner = subprocess.run) -> CheckResult:
    try:
        completed = runner(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-PnpDevice -Class Ports | Where-Object { $_.FriendlyName -like '*Bluetooth*' } "
                "| Format-Table -AutoSize | Out-String -Width 200",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return CheckResult("UNKNOWN", f"could not run Get-PnpDevice: {exc}")
    if completed.returncode != 0:
        return CheckResult(
            "UNKNOWN",
            f"Get-PnpDevice returned exit code {completed.returncode}: {completed.stderr.strip()}",
        )
    return CheckResult("PASS", completed.stdout.strip() or "no Bluetooth COM ports found")
