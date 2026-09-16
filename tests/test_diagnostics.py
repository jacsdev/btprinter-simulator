"""Tests for transport.diagnostics, backing `python main.py --doctor`.

Every check function is designed to be honest: PASS only for a check
actually performed, FAIL with a concrete remedy when it fails for real,
and UNKNOWN (never a fabricated PASS) when Windows gives no reliable
way to know. `subprocess.run` is injected so these tests never actually
shell out to `sc` or `powershell`.
"""

from __future__ import annotations

import subprocess

from transport.diagnostics import (
    CheckResult,
    check_discoverability,
    check_interpreter_restriction,
    check_rfcomm_socket_bind,
    check_sdp_registration,
    check_windows_service,
    list_bluetooth_com_ports,
    list_paired_bluetooth_devices,
)
from transport.rfcomm import SdpRegistrationError


class _FakeCompletedProcess:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_check_windows_service_passes_when_service_is_running():
    def fake_runner(*args, **kwargs):
        return _FakeCompletedProcess(stdout="SERVICE_NAME: bthserv\n        STATE : 4  RUNNING")

    result = check_windows_service("bthserv", runner=fake_runner)
    assert result.status == "PASS"
    assert "bthserv" in result.message


def test_check_windows_service_fails_with_remedy_when_stopped():
    def fake_runner(*args, **kwargs):
        return _FakeCompletedProcess(stdout="SERVICE_NAME: bthserv\n        STATE : 1  STOPPED")

    result = check_windows_service("bthserv", runner=fake_runner)
    assert result.status == "FAIL"
    assert "bthserv" in result.message


def test_check_windows_service_reports_unknown_when_subprocess_raises():
    def fake_runner(*args, **kwargs):
        raise FileNotFoundError("sc not found")

    result = check_windows_service("bthserv", runner=fake_runner)
    assert result.status == "UNKNOWN"
    assert "sc not found" in result.message or "sc query" in result.message


class _FakeDiagnosticSocket:
    """Fast-path fake: bind(0) immediately reports a real channel, so
    _bind_available_channel never needs to fall back to scanning."""

    def __init__(self, channel: int) -> None:
        self._channel = channel
        self.closed = False

    def bind(self, addr):
        pass

    def listen(self, backlog):
        pass

    def getsockname(self):
        return ("00:00:00:00:00:00", self._channel)

    def close(self):
        self.closed = True


def test_check_rfcomm_socket_bind_reports_pass_and_channel_on_success(monkeypatch):
    import transport.diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "_default_rfcomm_socket", lambda: _FakeDiagnosticSocket(6))
    result = check_rfcomm_socket_bind()
    assert result.status == "PASS"
    assert "6" in result.message


def test_check_rfcomm_socket_bind_reports_fail_on_oserror(monkeypatch):
    import transport.diagnostics as diagnostics

    def raising_socket():
        raise OSError("no Bluetooth radio")

    monkeypatch.setattr(diagnostics, "_default_rfcomm_socket", raising_socket)
    result = check_rfcomm_socket_bind()
    assert result.status == "FAIL"
    assert "no Bluetooth radio" in result.message


def test_check_sdp_registration_passes_and_reports_channel(monkeypatch):
    import transport.diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "_default_rfcomm_socket", lambda: _FakeDiagnosticSocket(8))
    monkeypatch.setattr(diagnostics, "register_sdp_service", lambda channel, name: None)
    monkeypatch.setattr(diagnostics, "deregister_sdp_service", lambda channel, name: None)

    result = check_sdp_registration()
    assert result.status == "PASS"
    assert "8" in result.message


def test_check_sdp_registration_fails_with_win32_error_code(monkeypatch):
    import transport.diagnostics as diagnostics

    def failing_register(channel, name):
        raise SdpRegistrationError(1231, "register")

    monkeypatch.setattr(diagnostics, "_default_rfcomm_socket", lambda: _FakeDiagnosticSocket(8))
    monkeypatch.setattr(diagnostics, "register_sdp_service", failing_register)

    result = check_sdp_registration()
    assert result.status == "FAIL"
    assert "1231" in result.message


def test_check_discoverability_is_always_unknown_with_manual_path():
    result = check_discoverability()
    assert result.status == "UNKNOWN"
    assert "Bluetooth & devices" in result.message


def test_list_paired_bluetooth_devices_passes_with_real_output():
    def fake_runner(*args, **kwargs):
        return _FakeCompletedProcess(stdout="FriendlyName\n------------\nMy Phone", returncode=0)

    result = list_paired_bluetooth_devices(runner=fake_runner)
    assert result.status == "PASS"
    assert "My Phone" in result.message


def test_list_paired_bluetooth_devices_unknown_on_nonzero_exit():
    def fake_runner(*args, **kwargs):
        return _FakeCompletedProcess(stderr="not recognized", returncode=1)

    result = list_paired_bluetooth_devices(runner=fake_runner)
    assert result.status == "UNKNOWN"


def test_list_bluetooth_com_ports_unknown_when_subprocess_raises():
    def fake_runner(*args, **kwargs):
        raise FileNotFoundError("powershell not found")

    result = list_bluetooth_com_ports(runner=fake_runner)
    assert result.status == "UNKNOWN"


def test_check_interpreter_restriction_warns_for_a_windowsapps_python():
    executable = (
        r"C:\Users\someone\AppData\Local\Microsoft\WindowsApps\\"
        r"PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\python.exe"
    )
    result = check_interpreter_restriction(executable=executable)
    assert result.status == "WARNING"
    assert "UNTESTED" in result.message
    assert "python.org" in result.message


def test_check_interpreter_restriction_passes_for_a_python_org_install():
    executable = r"C:\Python312\python.exe"
    result = check_interpreter_restriction(executable=executable)
    assert result.status == "PASS"


def test_check_interpreter_restriction_never_claims_a_confirmed_root_cause():
    executable = r"C:\Users\someone\AppData\Local\Microsoft\WindowsApps\python.exe"
    result = check_interpreter_restriction(executable=executable)
    assert "not a confirmed cause" in result.message.lower()


def test_check_result_is_a_frozen_dataclass_with_status_and_message():
    result = CheckResult(status="PASS", message="ok")
    assert result.status == "PASS"
    assert result.message == "ok"
