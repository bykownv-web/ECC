"""Ярлык J.A.R.V.I.S. на рабочем столе Windows (через WScript.Shell в PowerShell)."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
ICON = APP_DIR / "assets" / "jarvis.ico"

_PS = r"""
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'J.A.R.V.I.S.lnk'
$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = $env:JARVIS_TARGET
$s.Arguments = $env:JARVIS_ARGS
$s.WorkingDirectory = $env:JARVIS_WORKDIR
$s.IconLocation = $env:JARVIS_ICON
$s.Description = 'J.A.R.V.I.S. - assistant'
$s.Save()
Write-Output $lnk
"""


def _pythonw() -> Path:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def create_desktop_shortcut() -> str:
    """Создаёт или обновляет ярлык. Возвращает путь к ярлыку."""
    if platform.system() != "Windows":
        raise OSError("Ярлык создаётся только в Windows.")
    env = dict(
        os.environ,
        JARVIS_TARGET=str(_pythonw()),
        JARVIS_ARGS=f'"{APP_DIR / "main.py"}"',
        JARVIS_WORKDIR=str(APP_DIR),
        JARVIS_ICON=f"{ICON},0",
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS],
        env=env, capture_output=True, text=True, timeout=30, creationflags=0x08000000,
    )
    if result.returncode != 0:
        raise OSError((result.stderr or "не удалось создать ярлык").strip()[:300])
    return result.stdout.strip()


if __name__ == "__main__":
    print(create_desktop_shortcut())
