"""Unit tests for SdkVersionManager (pip subprocess mocked)."""

import subprocess
import sys

import pytest
from keboola.component.exceptions import UserException

from sdk_version_manager import SdkVersionManager


def test_pinned_is_noop_no_pip(monkeypatch):
    called = {"pip": False}

    def fake_run(*a, **k):
        called["pip"] = True
        raise AssertionError("pip should not be called for pinned")

    monkeypatch.setattr(subprocess, "run", fake_run)
    resolved = SdkVersionManager().ensure("pinned", "fail")
    assert called["pip"] is False
    # baked version is whatever is installed in the test venv
    assert resolved


def test_concrete_version_installs_and_prepends_path(monkeypatch, tmp_path):
    overlay = str(tmp_path / "overlay")
    captured = {}

    def fake_run(cmd, **k):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    mgr = SdkVersionManager(overlay_dir=overlay)
    monkeypatch.setattr(mgr, "_overlay_version", lambda: "0.2.105")

    resolved = mgr.ensure("0.2.105", "fail")
    assert resolved == "0.2.105"
    assert sys.path[0] == overlay
    assert "claude-agent-sdk==0.2.105" in captured["cmd"]
    assert "--target" in captured["cmd"]
    # cleanup the inserted path
    sys.path.remove(overlay)


def test_latest_uses_bare_package_spec(monkeypatch, tmp_path):
    overlay = str(tmp_path / "overlay2")
    captured = {}

    def fake_run(cmd, **k):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    mgr = SdkVersionManager(overlay_dir=overlay)
    monkeypatch.setattr(mgr, "_overlay_version", lambda: "0.2.200")

    mgr.ensure("latest", "fail")
    assert "claude-agent-sdk" in captured["cmd"]
    assert not any("==" in str(part) for part in captured["cmd"])
    sys.path.remove(overlay)


def test_install_failure_fail_mode_raises(monkeypatch, tmp_path):
    def fake_run(cmd, **k):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No matching distribution")

    monkeypatch.setattr(subprocess, "run", fake_run)
    mgr = SdkVersionManager(overlay_dir=str(tmp_path / "o"))
    with pytest.raises(UserException) as exc:
        mgr.ensure("9.9.9", "fail")
    assert "9.9.9" in str(exc.value)


def test_install_failure_fallback_pinned_returns_baked(monkeypatch, tmp_path):
    def fake_run(cmd, **k):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="registry down")

    monkeypatch.setattr(subprocess, "run", fake_run)
    mgr = SdkVersionManager(overlay_dir=str(tmp_path / "o"))
    resolved = mgr.ensure("9.9.9", "fallback_pinned")
    assert resolved  # baked version returned, no raise
