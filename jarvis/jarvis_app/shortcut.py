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
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
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


def shortcut_target() -> dict[str, str]:
    """Что запускает ярлык. В собранном Jarvis.exe (PyInstaller) — сам exe: main.py и папка
    _MEIPASS там не годятся (во временной папке их нет после выхода, а exe не принимает путь к скрипту)."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        return {"target": str(exe), "args": "", "workdir": str(exe.parent), "icon": f"{exe},0"}
    icon = ICON if ICON.exists() else _pythonw()
    return {
        "target": str(_pythonw()),
        "args": f'"{APP_DIR / "main.py"}"',
        "workdir": str(APP_DIR),
        "icon": f"{icon},0",
    }


def create_desktop_shortcut() -> str:
    """Создаёт или обновляет ярлык. Возвращает путь к ярлыку."""
    if platform.system() != "Windows":
        raise OSError("Ярлык создаётся только в Windows.")
    t = shortcut_target()
    env = dict(
        os.environ,
        JARVIS_TARGET=t["target"],
        JARVIS_ARGS=t["args"],
        JARVIS_WORKDIR=t["workdir"],
        JARVIS_ICON=t["icon"],
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        creationflags=0x08000000,
    )
    if result.returncode != 0:
        raise OSError((result.stderr or "не удалось создать ярлык").strip()[:300])
    path = (result.stdout or "").strip().splitlines()
    if not path:
        raise OSError("PowerShell не вернул путь к ярлыку.")
    return path[-1].strip()


if __name__ == "__main__":
    print(create_desktop_shortcut())
