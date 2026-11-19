"""Управление компьютером под Windows: программы, сайты, громкость, блокировка, выключение.

Запуск программ ограничен списком «apps» из настроек и программами, найденными в PATH,
чтобы ни голосовая команда, ни ответ ИИ не могли выполнить произвольную команду оболочки.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import webbrowser
from urllib.parse import quote_plus, urlparse

IS_WINDOWS = platform.system() == "Windows"

VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT = 0xB0
VK_MEDIA_PREV = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_KEYUP = 0x0002

KNOWN_SITES = {
    "ютуб": "https://www.youtube.com",
    "youtube": "https://www.youtube.com",
    "яндекс": "https://ya.ru",
    "почта": "https://mail.yandex.ru",
    "госзакупки": "https://zakupki.gov.ru",
    "закупки": "https://zakupki.gov.ru",
    "вк": "https://vk.com",
    "вконтакте": "https://vk.com",
    "карты": "https://yandex.ru/maps",
    "переводчик": "https://translate.yandex.ru",
}


class PCError(Exception):
    pass


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise PCError("Эта команда работает только в Windows.")


def _press_key(vk_code: int, times: int = 1) -> None:
    _require_windows()
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    for _ in range(max(1, times)):
        user32.keybd_event(vk_code, 0, 0, 0)
        user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)


def open_app(name: str, apps: dict[str, str]) -> str:
    key = (name or "").strip().lower()
    if not key:
        raise PCError("Не указано, какую программу открыть.")
    if key in KNOWN_SITES:
        return open_url(KNOWN_SITES[key])

    apps = {str(k).strip().lower(): str(v) for k, v in (apps or {}).items() if str(k).strip() and v}
    target = apps.get(key)
    if target is None:
        for alias, command in apps.items():
            # «открой ворд документ» → «ворд»; совпадение по куску короче 3 букв не считаем
            if (len(alias) >= 3 and alias in key) or (len(key) >= 3 and key in alias):
                target = command
                break
    if target is None:
        found = shutil.which(key) or shutil.which(f"{key}.exe")
        if found:
            target = found
    if target is None:
        raise PCError(
            f"Не знаю программу «{name}». Добавьте её в настройках, в разделе «Программы»."
        )

    _require_windows()
    try:
        os.startfile(target)  # type: ignore[attr-defined]
    except (OSError, AttributeError) as exc:
        raise PCError(f"Не удалось запустить «{name}»: {exc.strerror or exc}") from exc
    return f"Открываю {name}."


def open_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PCError("Можно открывать только адреса, начинающиеся с http:// или https://")
    webbrowser.open(parsed.geturl())
    return f"Открываю {parsed.netloc}."


def web_search(query: str) -> str:
    if not (query or "").strip():
        raise PCError("Пустой поисковый запрос.")
    webbrowser.open(f"https://ya.ru/search/?text={quote_plus(query.strip())}")
    return f"Ищу в интернете: {query}."


def volume(action: str, steps: int = 5) -> str:
    action = (action or "").strip().lower()
    try:
        steps = min(max(int(steps or 1), 1), 50)
    except (TypeError, ValueError):
        steps = 5
    if action == "up":
        _press_key(VK_VOLUME_UP, steps)
        return "Сделал громче."
    if action == "down":
        _press_key(VK_VOLUME_DOWN, steps)
        return "Сделал тише."
    if action == "mute":
        _press_key(VK_VOLUME_MUTE)
        return "Переключил звук."
    raise PCError("Неизвестное действие с громкостью.")


def media(action: str) -> str:
    codes = {"play_pause": VK_MEDIA_PLAY_PAUSE, "next": VK_MEDIA_NEXT, "prev": VK_MEDIA_PREV}
    action = (action or "").strip().lower()
    if action not in codes:
        raise PCError("Неизвестная медиа-команда.")
    _press_key(codes[action])
    return "Готово."


def lock_screen() -> str:
    _require_windows()
    ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]
    return "Блокирую компьютер."


def power(action: str, delay_sec: int = 60) -> str:
    """Выключение или перезагрузка с задержкой — её можно отменить командой cancel."""
    _require_windows()
    try:
        delay_sec = min(max(int(delay_sec), 30), 3600)
    except (TypeError, ValueError):
        delay_sec = 60
    action = (action or "").strip().lower()
    flags = {"shutdown": "/s", "restart": "/r"}
    if action not in flags and action != "cancel":
        raise PCError("Неизвестное действие питания.")
    try:
        if action == "cancel":
            subprocess.run(["shutdown", "/a"], check=False, creationflags=0x08000000)
            return "Выключение отменено."
        subprocess.run(["shutdown", flags[action], "/t", str(delay_sec)], check=False, creationflags=0x08000000)
    except (OSError, ValueError) as exc:
        raise PCError(f"Не удалось выполнить команду питания: {exc}") from exc
    verb = "Выключу" if action == "shutdown" else "Перезагружу"
    return f"{verb} компьютер через {delay_sec} секунд. Скажите «отмени выключение», чтобы отменить."


def system_info() -> str:
    try:
        import psutil
    except ImportError:
        return "Модуль psutil не установлен."
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    parts = [f"Процессор загружен на {cpu:.0f}%", f"память — на {mem.percent:.0f}%"]
    try:
        disk = psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\" if IS_WINDOWS else "/")
        parts.append(f"на диске свободно {disk.free / 1024**3:.0f} ГБ")
    except OSError:
        pass
    try:
        battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    except Exception:  # на некоторых ПК датчик батареи недоступен
        battery = None
    if battery is not None:
        parts.append(f"заряд батареи {battery.percent:.0f}%")
    return ", ".join(parts) + "."
