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
KEYRING_ELEVEN = "elevenlabs-api-key"

DEFAULT_CONFIG: dict[str, Any] = {
    "assistant_name": "Джарвис",
    "user_name": "сэр",
    # ollama — на этом ПК; gigachat — Сбер; openai — любой OpenAI-совместимый (Hermes, YandexGPT, DeepSeek…);
    # claude — Anthropic; none — без ИИ
    "ai_provider": "ollama",
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "qwen2.5:7b",
    "openai_preset": "hermes_openrouter",
    "openai_base_url": "https://openrouter.ai/api/v1",
    "openai_model": "nousresearch/hermes-4-70b",
    "gigachat_model": "GigaChat-2",
    "gigachat_scope": "GIGACHAT_API_PERS",
    "model": "claude-opus-5-5",
    "effort": "low",
    "voice_enabled": True,
    "tts_voice": "ru-RU-DmitryNeural",
    "tts_rate": "+0%",
    "tts_pitch": "-8Hz",
    "eleven_voice_id": "onwK4e9ZLuTAKqWW03F9",
    "wake_word": "джарвис",
    "city": "Москва",
    # Реквизиты для документов заявки
    "company": {
        "name": "", "inn": "", "kpp": "", "ogrn": "", "address": "",
        "director": "", "director_position": "Генеральный директор", "phone": "", "email": "",
    },
    "telegram": {"enabled": False, "chat_id": None, "bot_name": "", "notify": True},
    "email": {"enabled": False, "preset": "yandex", "host": "smtp.yandex.ru", "port": 465, "login": "", "to": "", "notify": True},
    "analyst": {
        "enabled": False,
        "profile": (
            "Поставка, монтаж и пусконаладка проекторов, интерактивного и мультимедийного оборудования, "
            "звукового, светового и сценического (театрального) оборудования для школ, вузов, домов культуры и театров."
        ),
        "min_score": 60,
        "top_n": 3,
    },
    "shortcut_created": False,
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
        "pages_per_tag": 2,
        "strict": True,  # в названии закупки должны быть все слова тега
        "minus_words": None,  # None — стандартный список (медицина, дороги, продукты и т.п.)
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


def get_elevenlabs_key() -> str:
    try:
        import keyring

        stored = keyring.get_password(KEYRING_SERVICE, KEYRING_ELEVEN)
        if stored:
            return stored
    except Exception:
        pass
    return os.environ.get("ELEVENLABS_API_KEY", "")


def set_elevenlabs_key(value: str) -> bool:
    try:
        import keyring

        if value:
            keyring.set_password(KEYRING_SERVICE, KEYRING_ELEVEN, value)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, KEYRING_ELEVEN)
            except Exception:
                pass
        return True
    except Exception:
        return False
