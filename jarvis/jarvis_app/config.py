"""Настройки приложения: хранятся в %APPDATA%\\Jarvis\\config.json.

API-ключ Claude хранится отдельно — в диспетчере учётных данных Windows (keyring),
а если keyring недоступен, берётся из переменной окружения ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

APP_NAME = "Jarvis"
KEYRING_SERVICE = "jarvis-assistant"
KEYRING_USER = "anthropic-api-key"

DEFAULT_CONFIG: dict[str, Any] = {
    "assistant_name": "Джарвис",
    "user_name": "сэр",
    "model": "claude-opus-5-5",
    "effort": "low",
    "voice_enabled": True,
    "wake_word": "джарвис",
    "city": "Москва",
    "procurement": {
        "tags": [
            "проектор",
            "мультимедиа",
            "звуковое оборудование",
            "оборудование для театра",
        ],
        "fz44": True,
        "fz223": True,
        "only_active": True,
        "price_from": None,
        "price_to": None,
        "pages_per_tag": 1,
        "monitor_interval_min": 30,
        "ca_bundle": "",
    },
    # Программы, которые Джарвис может открывать по названию.
    # Ключ — как вы её называете, значение — команда или полный путь к .exe.
    "apps": {
        "блокнот": "notepad.exe",
        "калькулятор": "calc.exe",
        "проводник": "explorer.exe",
        "паинт": "mspaint.exe",
        "диспетчер задач": "taskmgr.exe",
        "командная строка": "cmd.exe",
        "параметры": "ms-settings:",
        "word": "winword.exe",
        "ворд": "winword.exe",
        "excel": "excel.exe",
        "эксель": "excel.exe",
    },
}


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def documents_dir() -> Path:
    path = Path.home() / "Documents" / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    """Потокобезопасная обёртка над config.json."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (app_data_dir() / "config.json")
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            try:
                stored = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    return _deep_merge(DEFAULT_CONFIG, stored)
            except (OSError, json.JSONDecodeError):
                pass
        return copy.deepcopy(DEFAULT_CONFIG)

    def save(self) -> None:
        with self._lock:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def update(self, values: dict[str, Any]) -> None:
        with self._lock:
            self._data = _deep_merge(self._data, values)
        self.save()

    def set(self, key: str, value: Any) -> None:
        """Заменяет значение целиком (без слияния словарей)."""
        with self._lock:
            self._data[key] = copy.deepcopy(value)
        self.save()

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)


def get_api_key() -> str:
    try:
        import keyring

        stored = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        if stored:
            return stored
    except Exception:  # keyring может отсутствовать или не иметь бэкенда
        pass
    return os.environ.get("ANTHROPIC_API_KEY", "")


def set_api_key(value: str) -> bool:
    """Сохраняет ключ в диспетчере учётных данных Windows. Возвращает успех."""
    try:
        import keyring

        if value:
            keyring.set_password(KEYRING_SERVICE, KEYRING_USER, value)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
            except Exception:
                pass
        return True
    except Exception:
        return False
