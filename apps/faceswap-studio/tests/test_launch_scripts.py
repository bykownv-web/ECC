"""Sanity checks for the one-click install/run scripts (install.bat, run.bat, install.sh, ...)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
INSTALL_PS1 = APP_DIR / "scripts" / "install.ps1"
BAT_FILES = ["install.bat", "run.bat", "run-mock.bat"]
SH_FILES = ["install.sh", "run.sh"]


@pytest.mark.parametrize("name", [*BAT_FILES, *SH_FILES, "scripts/install.ps1", ".gitattributes"])
def test_script_exists(name):
    assert (APP_DIR / name).is_file(), name


def test_install_ps1_has_utf8_bom():
    # PowerShell 5.1 reads BOM-less scripts as ANSI and garbles Cyrillic messages.
    assert INSTALL_PS1.read_bytes().startswith(b"\xef\xbb\xbf")


def test_install_ps1_is_powershell_51_compatible():
    text = INSTALL_PS1.read_text(encoding="utf-8-sig")
    # Pipeline chain operators, ternary and null-coalescing exist only in PowerShell 7.
    assert "&&" not in text
    assert "||" not in text
    assert "??" not in text


def test_install_ps1_uses_only_pytorch_index_for_torch():
    text = INSTALL_PS1.read_text(encoding="utf-8-sig")
    assert "--index-url" in text
    # An extra PyPI index would let pip pick the CPU-only torch wheel on Windows.
    assert "--extra-index-url" not in text
    assert "https://download.pytorch.org/whl/" in text


@pytest.mark.parametrize("name", [*BAT_FILES, "scripts/install.ps1"])
def test_windows_scripts_use_crlf(name):
    data = (APP_DIR / name).read_bytes()
    assert b"\r\n" in data
    assert data.count(b"\n") == data.count(b"\r\n"), f"{name} has bare LF line endings"


@pytest.mark.parametrize("name", SH_FILES)
def test_shell_scripts_use_lf(name):
    assert b"\r\n" not in (APP_DIR / name).read_bytes()


def test_install_bat_wraps_powershell_and_pauses():
    text = (APP_DIR / "install.bat").read_text(encoding="utf-8")
    assert '-ExecutionPolicy Bypass -File "%~dp0scripts\\install.ps1" %*' in text
    assert "pause" in text


def test_run_bat_checks_venv_and_opens_browser():
    text = (APP_DIR / "run.bat").read_text(encoding="utf-8")
    assert 'cd /d "%~dp0"' in text
    assert ".venv\\Scripts\\python.exe" in text
    assert "install.bat" in text
    assert "http://127.0.0.1:8000" in text
    assert "-m app.main" in text
    assert "FSS_BACKEND=mock" in (APP_DIR / "run-mock.bat").read_text(encoding="utf-8")


def test_gitattributes_line_endings():
    text = (APP_DIR / ".gitattributes").read_text(encoding="utf-8")
    for rule in ("*.bat text eol=crlf", "*.ps1 text eol=crlf", "*.sh text eol=lf"):
        assert rule in text


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_shell_scripts_parse():
    result = subprocess.run(
        ["bash", "-n", *SH_FILES], cwd=APP_DIR, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name == "nt", reason="executable bit is not tracked on Windows checkouts")
@pytest.mark.parametrize("name", SH_FILES)
def test_shell_scripts_are_executable(name):
    assert os.access(APP_DIR / name, os.X_OK)


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not available")
def test_install_ps1_parses_in_powershell():
    script = (
        "$tokens = $null; $errors = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{INSTALL_PS1}', [ref]$tokens, [ref]$errors); "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { $_.ToString() }; exit 1 }"
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
