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


class _FakeSys:
    """Minimal stand-in for the `sys` module used by the interpreter
    restriction check, so tests can fake the platform without mutating
    the real process-wide `sys.platform`."""

    def __init__(self, platform: str) -> None:
        self.platform = platform


class _FakeCtypesValue:
    def __init__(self, value: int) -> None:
        self.value = value


class _FakeCtypesToken:
    """Minimal stand-in for the `ctypes` API surface used by
    check_interpreter_restriction, so the token query can be exercised
    without touching the real process token."""

    def __init__(
        self,
        flag_value: int = 0,
        open_fails: bool = False,
        token_info_fails: bool = False,
    ) -> None:
        self.flag_value = flag_value
        self.open_fails = open_fails
        self.token_info_fails = token_info_fails
        self.windll = self
        self.advapi32 = self
        self.kernel32 = self

    def GetCurrentProcess(self) -> int:
        return 0x1234

    def OpenProcessToken(self, process, access, token_ref) -> int:
        return 0 if self.open_fails else 1

    def GetTokenInformation(self, token, info_class, flag_ref, size, returned_ref) -> int:
        if self.token_info_fails:
            return 0
        flag_ref.value = self.flag_value
        return 1

    def WinError(self):
        raise OSError("simulated Win32 token error")

    def c_void_p(self):
        return _FakeCtypesValue(0)

    def c_ulong(self, value: int = 0):
        return _FakeCtypesValue(value)

    def byref(self, obj):
        return obj

    def sizeof(self, obj) -> int:
        return 4


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


def test_check_sdp_registration_tries_blob_first_and_reports_blob_record(monkeypatch):
    import transport.diagnostics as diagnostics

    calls = []

    def fake_blob_register(channel, name):
        calls.append(("blob_register", channel))
        return 999

    def fake_blob_deregister(handle):
        calls.append(("blob_deregister", handle))

    def simple_register_should_not_run(channel, name):
        raise AssertionError("simple register must not be called when blob succeeds")

    monkeypatch.setattr(diagnostics, "_default_rfcomm_socket", lambda: _FakeDiagnosticSocket(8))
    monkeypatch.setattr(diagnostics, "register_sdp_service_blob", fake_blob_register)
    monkeypatch.setattr(diagnostics, "deregister_sdp_service_blob", fake_blob_deregister)
    monkeypatch.setattr(diagnostics, "register_sdp_service", simple_register_should_not_run)

    result = check_sdp_registration()

    assert result.status == "PASS"
    assert "(blob record)" in result.message
    assert "8" in result.message
    assert calls == [("blob_register", 8), ("blob_deregister", 999)]


def test_check_sdp_registration_falls_back_to_simple_when_blob_fails(monkeypatch):
    import transport.diagnostics as diagnostics

    calls = []

    def failing_blob_register(channel, name):
        calls.append(("blob_register", channel))
        raise SdpRegistrationError(1231, "blob register")

    def fake_simple_register(channel, name):
        calls.append(("simple_register", channel))

    def fake_simple_deregister(channel, name):
        calls.append(("simple_deregister", channel))

    monkeypatch.setattr(diagnostics, "_default_rfcomm_socket", lambda: _FakeDiagnosticSocket(8))
    monkeypatch.setattr(diagnostics, "register_sdp_service_blob", failing_blob_register)
    monkeypatch.setattr(
        diagnostics,
        "deregister_sdp_service_blob",
        lambda handle: calls.append(("blob_deregister", handle)),
    )
    monkeypatch.setattr(diagnostics, "register_sdp_service", fake_simple_register)
    monkeypatch.setattr(diagnostics, "deregister_sdp_service", fake_simple_deregister)

    result = check_sdp_registration()

    assert result.status == "PASS"
    assert "(simple record)" in result.message
    assert "8" in result.message
    assert ("blob_register", 8) in calls
    assert ("simple_register", 8) in calls
    assert ("simple_deregister", 8) in calls
    assert not any(call[0] == "blob_deregister" for call in calls)


def test_check_sdp_registration_fails_with_last_error_when_both_paths_fail(monkeypatch):
    import transport.diagnostics as diagnostics

    def failing_blob_register(channel, name):
        raise SdpRegistrationError(1231, "blob register")

    def failing_simple_register(channel, name):
        raise SdpRegistrationError(9999, "simple register")

    monkeypatch.setattr(diagnostics, "_default_rfcomm_socket", lambda: _FakeDiagnosticSocket(8))
    monkeypatch.setattr(diagnostics, "register_sdp_service_blob", failing_blob_register)
    monkeypatch.setattr(diagnostics, "register_sdp_service", failing_simple_register)

    result = check_sdp_registration()

    assert result.status == "FAIL"
    assert "9999" in result.message


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


def test_check_interpreter_restriction_returns_unknown_on_non_windows(monkeypatch):
    import transport.diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "sys", _FakeSys(platform="linux"))
    result = check_interpreter_restriction()

    assert result.status == "UNKNOWN"
    assert "Windows-only" in result.message


def test_check_interpreter_restriction_warns_when_token_flag_is_nonzero(monkeypatch):
    import transport.diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "sys", _FakeSys(platform="win32"))
    monkeypatch.setattr(diagnostics, "ctypes", _FakeCtypesToken(flag_value=1))

    result = check_interpreter_restriction()

    assert result.status == "WARNING"
    assert "AppContainer" in result.message
    assert "not a confirmed cause" in result.message.lower()


def test_check_interpreter_restriction_passes_when_token_flag_is_zero(monkeypatch):
    import transport.diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "sys", _FakeSys(platform="win32"))
    monkeypatch.setattr(diagnostics, "ctypes", _FakeCtypesToken(flag_value=0))

    result = check_interpreter_restriction()

    assert result.status == "PASS"
    assert "not AppContainer-restricted" in result.message


def test_check_interpreter_restriction_returns_unknown_when_open_process_token_fails(monkeypatch):
    import transport.diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "sys", _FakeSys(platform="win32"))
    monkeypatch.setattr(diagnostics, "ctypes", _FakeCtypesToken(open_fails=True))

    result = check_interpreter_restriction()

    assert result.status == "UNKNOWN"
    assert "could not open the process token" in result.message


def test_check_interpreter_restriction_returns_unknown_when_get_token_information_fails(monkeypatch):
    import transport.diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "sys", _FakeSys(platform="win32"))
    monkeypatch.setattr(diagnostics, "ctypes", _FakeCtypesToken(token_info_fails=True))

    result = check_interpreter_restriction()

    assert result.status == "UNKNOWN"
    assert "could not read the AppContainer token flag" in result.message


def test_check_result_is_a_frozen_dataclass_with_status_and_message():
    result = CheckResult(status="PASS", message="ok")
    assert result.status == "PASS"
    assert result.message == "ok"
