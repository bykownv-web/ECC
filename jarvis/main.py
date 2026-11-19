"""Точка входа: python main.py"""

from __future__ import annotations

import sys
from pathlib import Path

ERROR_ALREADY_EXISTS = 183
_MUTEX_NAME = "Local\\JarvisAssistantSingleInstance"
_mutex_handle = None  # держим ссылку, пока работает программа


def resource_path(relative: str) -> Path:
    """Путь к файлам интерфейса и в обычном запуске, и в собранном .exe (PyInstaller)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def already_running() -> bool:
    """Второе окно Джарвиса опрашивало бы того же Telegram-бота — Telegram отвечает 409 обоим.
    Поэтому в Windows разрешаем только один экземпляр (именованный мьютекс)."""
    global _mutex_handle
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        _mutex_handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        return bool(_mutex_handle) and kernel32.GetLastError() == ERROR_ALREADY_EXISTS
    except (AttributeError, OSError):
        return False


def _message(text: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "J.A.R.V.I.S.", 0x40)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        print(text, file=sys.stderr)


def main() -> None:
    if already_running():
        _message("Джарвис уже запущен. Найдите его окно на панели задач.")
        return
    try:
        import webview
    except ImportError:
        _message("Не установлен pywebview. Запустите run.bat — он установит всё нужное.")
        return

    from jarvis_app.api import JarvisApi
    from jarvis_app.config import Config

    index = resource_path("ui/index.html")
    if not index.exists():
        _message(f"Не найден интерфейс: {index}")
        return
    config = Config()
    api = JarvisApi(config)
    window = webview.create_window(
        "J.A.R.V.I.S.",
        url=str(index),
        js_api=api,
        width=1280,
        height=820,
        min_size=(900, 620),
        background_color="#05080d",
    )
    api._attach(window)  # noqa: SLF001
    window.events.closed += api.shutdown
    window.events.loaded += api.ensure_shortcut
    webview.start()


if __name__ == "__main__":
    main()
