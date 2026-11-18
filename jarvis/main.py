"""Точка входа: python main.py"""

from __future__ import annotations

import sys
from pathlib import Path

import webview

from jarvis_app.api import JarvisApi
from jarvis_app.config import Config


def resource_path(relative: str) -> Path:
    """Путь к файлам интерфейса и в обычном запуске, и в собранном .exe (PyInstaller)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def main() -> None:
    config = Config()
    api = JarvisApi(config)
    window = webview.create_window(
        "J.A.R.V.I.S.",
        url=str(resource_path("ui/index.html")),
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
