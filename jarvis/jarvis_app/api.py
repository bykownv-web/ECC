"""Мост между окном (HTML/JS) и Python. Методы класса JarvisApi вызываются из JS через pywebview."""

from __future__ import annotations

import functools
import inspect
import json
import subprocess
import threading
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import __version__
from .analyst import Analyst, format_report
from .brain import Brain
from .config import (
    Config,
    app_data_dir,
    get_api_key,
    get_elevenlabs_key,
    set_api_key,
    set_elevenlabs_key,
)
from .notify import (
    REMOTE_BLOCKED_TOOLS,
    SMTP_KEY,
    SMTP_PRESETS,
    Mailer,
    Notifier,
    TelegramBot,
    TelegramError,
    get_secret,
    set_secret,
)
from .procurement import ProcurementError, summary_text
from .relevance import DEFAULT_MINUS_WORDS
from .tender import TenderError, TenderWorkspace
from .tools import ToolBox, ToolError, to_bool, to_int, to_number
from .voice import Listener, Speaker, VoiceError

TOOL_LABELS = {
    "search_procurements": "ищу закупки в ЕИС",
    "prepare_tender": "разбираю закупку",
    "procurement_analytics": "анализирую закупки",
    "export_procurements": "выгружаю в Excel",
    "open_app": "открываю программу",
    "open_url": "открываю сайт",
    "web_search": "ищу в интернете",
    "set_volume": "меняю громкость",
    "media_control": "управляю плеером",
    "lock_screen": "блокирую экран",
    "power": "управляю питанием",
    "get_time": "смотрю время",
    "get_weather": "узнаю погоду",
    "set_reminder": "ставлю напоминание",
    "list_reminders": "смотрю напоминания",
    "system_info": "проверяю систему",
}

COMPANY_KEYS = {"name", "inn", "kpp", "ogrn", "address", "director", "director_position", "phone", "email"}
SETTINGS_KEYS = {
    "assistant_name", "user_name", "model", "effort", "voice_enabled", "wake_word", "city", "apps",
    "tts_voice", "tts_rate", "tts_pitch", "eleven_voice_id",
    "ai_provider", "ollama_url", "ollama_model",
    "openai_preset", "openai_base_url", "openai_model", "gigachat_model", "gigachat_scope",
}
AI_PROVIDERS = {"ollama", "gigachat", "openai", "claude", "none"}
PROCUREMENT_KEYS = {"strict", "minus_words", "tags", "fz44", "fz223", "only_active", "price_from", "price_to", "pages_per_tag",
                    "monitor_interval_min", "ca_bundle"}


def _log_error(exc: BaseException) -> None:
    try:
        with open(app_data_dir() / "error.log", "a", encoding="utf-8") as fh:
            fh.write("".join(traceback.format_exception(exc)) + "\n")
    except OSError:
        pass


def _error_text(exc: BaseException) -> str:
    detail = str(exc).strip()
    return f"Ошибка: {detail[:300]}" if detail else f"Ошибка: {exc.__class__.__name__}. Подробности в error.log."


def _jsonable(value: Any) -> Any:
    """pywebview отдаёт результат в JS через JSON: Path, set и прочее превращаем в строки заранее."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _js_safe(func):
    """Обёртка публичного метода, который вызывается из JS: ошибка не улетает в окно необработанной.

    Методы, возвращающие словарь, отвечают {"ok": False, "text": …}; остальные показывают ошибку в чате.
    Сигнатура сохраняется — pywebview читает имена параметров.
    """
    returns_dict = str(func.__annotations__.get("return", "")).startswith("dict")

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return _jsonable(func(self, *args, **kwargs))
        except Exception as exc:
            _log_error(exc)
            text = _error_text(exc)
            if returns_dict:
                return {"ok": False, "text": text}
            self._emit("error", {"text": text})
            return None

    wrapper.__signature__ = inspect.signature(func)
    return wrapper


def _js_api(cls):
    for name, value in list(vars(cls).items()):
        if not name.startswith("_") and inspect.isfunction(value):
            setattr(cls, name, _js_safe(value))
    return cls


def _clean_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        return []
    tags: list[str] = []
    for item in value:
        tag = " ".join(str(item).split())[:100] if item is not None else ""
        if tag and tag.lower() not in (t.lower() for t in tags):
            tags.append(tag)
    return tags[:50]


def clean_procurement(values: Any) -> dict[str, Any]:
    """Настройки поиска из окна: только известные ключи и правильные типы."""
    if not isinstance(values, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key not in PROCUREMENT_KEYS:
            continue
        if key == "tags":
            out[key] = _clean_tags(value)
        elif key in ("fz44", "fz223", "only_active", "strict"):
            out[key] = to_bool(value, True)
        elif key in ("price_from", "price_to"):
            number = to_number(value, None)
            out[key] = number if number and number > 0 else None
        elif key == "pages_per_tag":
            out[key] = max(1, min(10, to_int(value, 2)))
        elif key == "monitor_interval_min":
            out[key] = max(10, min(24 * 60, to_int(value, 30)))
        elif key == "minus_words":
            # None — стандартный список; пустой список — пользователь осознанно убрал все минус-слова
            if value is None:
                out[key] = None
            elif isinstance(value, str):
                out[key] = [w.strip() for w in value.replace(";", ",").replace("\n", ",").split(",") if w.strip()]
            elif isinstance(value, (list, tuple)):
                out[key] = [str(w).strip() for w in value if w is not None and str(w).strip()]
        elif key == "ca_bundle":
            out[key] = str(value or "").strip()
    return out


class RemoteToolBox:
    """Инструменты для команд из Telegram: то же, что в окне, кроме опасных действий."""

    def __init__(self, toolbox: ToolBox) -> None:
        self._toolbox = toolbox

    def execute(self, name: str, args: dict[str, Any]) -> str:
        if name in REMOTE_BLOCKED_TOOLS:
            raise ToolError("Это действие нельзя выполнить из Telegram — только с компьютера.")
        if name == "prepare_tender" and self._toolbox.tender_runner is not None:
            return self._toolbox.tender_runner(str((args or {}).get("number") or ""), remote=True)
        return self._toolbox.execute(name, args)

    def __getattr__(self, item: str):
        if item == "_toolbox":  # ещё не задан (копирование, pickle) — без бесконечной рекурсии
            raise AttributeError(item)
        return getattr(self._toolbox, item)


@_js_api
class JarvisApi:
    def __init__(self, config: Config) -> None:
        # Сначала все поля, потом фоновые потоки: Telegram и мониторинг сразу обращаются к ним.
        self._config = config
        self._window = None
        self._busy = threading.Event()
        self._monitor_stop: threading.Event | None = None
        self._monitor_lock = threading.Lock()
        self._shortcut_started = False
        self._listener: Listener | None = None
        self._voice_error = ""
        self._toolbox = ToolBox(config, self._emit)
        self._toolbox.on_reminder = self._on_reminder
        self._tender = TenderWorkspace(config, self._emit)
        self._brain = Brain(config, self._toolbox, on_tool=self._on_tool)
        self._speaker = Speaker()
        self._speaker.eleven_key = get_elevenlabs_key
        self._speaker.on_error = lambda text: self._emit("error", {"text": text})
        self._apply_voice_settings()
        self._remote_brain = Brain(config, RemoteToolBox(self._toolbox))  # Telegram — отдельная история диалога
        self._telegram = TelegramBot(config, self._on_telegram_message, self._emit)
        self._mailer = Mailer(config)
        self._notifier = Notifier(self._telegram, self._mailer, config, on_error=lambda text: self._emit("error", {"text": text}))
        self._analyst = Analyst(config, self._toolbox.procurement_client, self._emit, notify=self._notify_external)
        self._toolbox.tender_runner = self._tender_from_chat
        try:
            self._listener = Listener()
        except VoiceError as exc:
            self._voice_error = str(exc)
        except Exception as exc:  # сломанный PyAudio/SpeechRecognition не должен мешать запуску окна
            _log_error(exc)
            self._voice_error = f"Голосовой ввод недоступен ({exc.__class__.__name__})."
        self._start_background()

    def _start_background(self) -> None:
        try:
            self._telegram.start()
        except Exception as exc:
            _log_error(exc)
        if (self._config.get("analyst", {}) or {}).get("enabled"):
            self.set_monitoring(True)

    # ---------- служебное ----------
    def _attach(self, window) -> None:
        self._window = window

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._window is None:
            return
        data = json.dumps({"event": event, "payload": payload}, ensure_ascii=True, default=str)
        try:
            self._window.evaluate_js(f"window.jarvis && window.jarvis.onEvent({data})")
        except Exception:
            pass

    def _on_tool(self, name: str) -> None:
        self._emit("tool", {"text": TOOL_LABELS.get(name, name)})

    def _run_async(self, target, *args) -> None:
        def wrapper() -> None:
            try:
                target(*args)
            except Exception as exc:  # фоновые ошибки не должны ронять окно, но и молчать нельзя
                _log_error(exc)
                self._emit("error", {"text": _error_text(exc)})

        threading.Thread(target=wrapper, daemon=True).start()

    def _on_reminder(self, text: str) -> None:
        """Напоминание сработало: окно уже получило событие; ещё говорим вслух и пишем в Telegram."""
        user = str(self._config.get("user_name", "сэр") or "сэр")
        self._speaker.say(f"{user.capitalize()}, напоминаю: {text}")
        if self._telegram.chat_id():
            self._telegram.send(f"⏰ Напоминание: {text}")

    # ---------- состояние и настройки ----------
    def get_state(self) -> dict[str, Any]:
        def safe(fn, default):
            try:
                return fn()
            except Exception as exc:  # одна сломанная часть не должна оставлять окно без настроек
                _log_error(exc)
                return default

        cfg = self._config.as_dict()
        return {
            "config": cfg,
            "ai_label": safe(self._ai_label, "ИИ: простой режим"),
            "ai_ok": safe(self._brain.has_ai, False),
            "has_key": bool(safe(get_api_key, "")),
            "has_eleven_key": bool(safe(get_elevenlabs_key, "")),
            "has_openai_key": bool(get_secret("openai-compatible-api-key")),
            "has_gigachat_key": bool(get_secret("gigachat-auth-key")),
            "default_minus_words": DEFAULT_MINUS_WORDS,
            "telegram": safe(self._telegram.status, {}),
            "email": safe(lambda: {"has_password": bool(get_secret(SMTP_KEY)), "configured": self._mailer.configured()}, {}),
            "monitoring": self._monitor_stop is not None,
            "voice_available": self._listener is not None,
            "voice_error": self._voice_error,
            "version": __version__,
            "greeting": safe(self._greeting, "Чем могу помочь?"),
        }

    def get_system_stats(self) -> dict[str, Any]:
        """Телеметрия для панели ядра: загрузка процессора, памяти и диска в процентах."""
        try:
            import os

            import psutil

            drive = (os.environ.get("SystemDrive", "C:") + "\\") if os.name == "nt" else "/"
            return {
                "cpu": psutil.cpu_percent(interval=None),
                "ram": psutil.virtual_memory().percent,
                "disk": psutil.disk_usage(drive).percent,
            }
        except Exception:
            return {"cpu": None, "ram": None, "disk": None}

    def save_ai_key(self, provider: str, key: str) -> dict[str, Any]:
        from .cloud_llm import GIGACHAT_KEY, OPENAI_KEY

        name = {"openai": OPENAI_KEY, "gigachat": GIGACHAT_KEY}.get(provider)
        if not name:
            return {"ok": False, "text": "Неизвестный сервис."}
        if not set_secret(name, (key or "").strip()):
            return {"ok": False, "text": "Не удалось сохранить ключ в диспетчере учётных данных Windows."}
        self._brain.reset()
        return {"ok": True, "text": "Ключ сохранён." if key else "Ключ удалён."}

    def test_ai(self) -> None:
        """Короткий запрос к выбранной модели: проверка ключа, адреса и скорости."""
        def work() -> None:
            import time as _t

            from .llm import LLM, LLMError

            started = _t.time()
            try:
                LLM(self._config).json("Ответь JSON.", 'Верни {"ok": true}', {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                                                            "required": ["ok"], "additionalProperties": False})
                self._emit("toast", {"text": f"ИИ отвечает: {self._ai_label()}, {(_t.time() - started):.1f} с."})
            except LLMError as exc:
                self._emit("error", {"text": f"Проверка ИИ: {exc}"})

        self._run_async(work)

    def _ai_label(self) -> str:
        provider = self._brain.provider()
        if provider == "gigachat":
            return f"ИИ: GigaChat ({self._config.get('gigachat_model', '')})"
        if provider == "openai":
            return f"ИИ: {self._config.get('openai_model', '')}"
        if provider == "ollama":
            return f"ИИ: Ollama ({self._config.get('ollama_model', '')})"
        if provider == "claude" and get_api_key():
            return "ИИ: Claude"
        return "ИИ: простой режим"

    def _greeting(self) -> str:
        user = self._config.get("user_name", "сэр")
        if self._brain.has_ai():
            return f"Системы в норме. Чем могу помочь, {user}?"
        return f"Здравствуйте, {user}. Работаю в простом режиме — включите ИИ в настройках."

    def check_ollama(self) -> dict[str, Any]:
        from .ollama_brain import list_models

        models = list_models(str(self._config.get("ollama_url", "")))
        if not models:
            return {"ok": False, "models": [], "text": "Ollama не отвечает. Установите и запустите её (см. README)."}
        current = self._config.get("ollama_model", "")
        note = "" if current in models else f" Модель {current} не скачана: ollama pull {current}"
        return {"ok": True, "models": models, "text": "Ollama работает. Модели: " + ", ".join(models) + "." + note}

    def save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        values = values if isinstance(values, dict) else {}
        clean: dict[str, Any] = {k: v for k, v in values.items() if k in SETTINGS_KEYS}
        if "voice_enabled" in clean:
            clean["voice_enabled"] = to_bool(clean["voice_enabled"], True)
        for text_key in SETTINGS_KEYS - {"apps", "voice_enabled"}:
            if text_key in clean:
                clean[text_key] = "" if clean[text_key] is None else str(clean[text_key]).strip()[:500]
        for required in ("assistant_name", "user_name", "wake_word", "city", "ai_provider", "model"):
            if required in clean and not clean[required]:
                clean.pop(required)  # пустое имя или слово-активатор ломают работу — оставляем прежнее
        if "ai_provider" in clean and clean["ai_provider"] not in AI_PROVIDERS:
            clean.pop("ai_provider")
        for url_key in ("ollama_url", "openai_base_url"):
            if url_key in clean and not str(clean[url_key]).startswith(("http://", "https://")):
                clean.pop(url_key)
        if clean.get("gigachat_scope") not in (None, "GIGACHAT_API_PERS", "GIGACHAT_API_B2B", "GIGACHAT_API_CORP"):
            clean.pop("gigachat_scope")
        if isinstance(clean.get("apps"), dict):
            clean["apps"] = {
                str(k).strip().lower(): str(v).strip()
                for k, v in clean["apps"].items()
                if str(k).strip() and str(v).strip()
            }
        else:
            clean.pop("apps", None)
        company = values.get("company")
        if isinstance(company, dict):
            clean["company"] = {k: ("" if v is None else str(v).strip()[:300]) for k, v in company.items() if k in COMPANY_KEYS}
        proc = clean_procurement(values.get("procurement"))
        if proc:
            clean["procurement"] = proc
        old_interval = (self._config.get("procurement", {}) or {}).get("monitor_interval_min")
        if "apps" in clean:
            # Список программ заменяется целиком, чтобы удалённые пункты не возвращались.
            self._config.set("apps", clean.pop("apps"))
        self._config.update(clean)
        self._apply_voice_settings()
        if "monitor_interval_min" in proc and proc["monitor_interval_min"] != old_interval and self._monitor_stop is not None:
            self.set_monitoring(True)  # новый интервал вступает в силу сразу
        return self.get_state()

    def _apply_voice_settings(self) -> None:
        self._speaker.enabled = bool(self._config.get("voice_enabled", True))
        self._speaker.configure(
            str(self._config.get("tts_voice", "ru-RU-DmitryNeural")),
            str(self._config.get("tts_rate", "+0%")),
            str(self._config.get("tts_pitch", "-8Hz")),
            str(self._config.get("eleven_voice_id", "")),
        )

    def test_voice(self) -> None:
        name = self._config.get("assistant_name", "Джарвис")
        user = self._config.get("user_name", "сэр")
        self._speaker.say(f"Добрый день, {user}. Я {name}. Все системы работают в штатном режиме.")

    def save_api_key(self, key: str) -> dict[str, Any]:
        key = (key or "").strip()
        if key and not key.startswith("sk-ant-"):
            return {"ok": False, "text": "Ключ Claude обычно начинается с sk-ant-."}
        ok = set_api_key(key)
        if not ok:
            return {"ok": False, "text": "Не удалось сохранить ключ в диспетчере учётных данных Windows."}
        self._brain.reset()
        return {"ok": True, "text": "Ключ сохранён." if key else "Ключ удалён."}

    def save_elevenlabs_key(self, key: str) -> dict[str, Any]:
        key = (key or "").strip()
        if key and (len(key) < 20 or any(c.isspace() for c in key)):
            return {"ok": False, "text": "Это не похоже на ключ ElevenLabs."}
        if not set_elevenlabs_key(key):
            return {"ok": False, "text": "Не удалось сохранить ключ в диспетчере учётных данных Windows."}
        self._apply_voice_settings()
        return {"ok": True, "text": "Ключ ElevenLabs сохранён." if key else "Ключ ElevenLabs удалён."}

    # ---------- разговор ----------
    def send_message(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if self._busy.is_set():
            self._emit("error", {"text": "Я ещё обрабатываю предыдущий запрос."})
            return
        self._busy.set()
        self._run_async(self._process, text)

    def _process(self, text: str) -> None:
        try:
            self._speaker.stop()
            self._emit("state", {"mode": "thinking"})
            answer = self._brain.ask(text)
            self._emit("assistant", {"text": answer})
            self._speaker.say(answer)
        finally:
            self._busy.clear()
            self._emit("state", {"mode": "idle"})

    def reset_chat(self) -> None:
        self._brain.reset()

    def stop_speaking(self) -> None:
        self._speaker.stop()

    def listen_once(self) -> None:
        if self._listener is None:
            self._emit("error", {"text": self._voice_error or "Голосовой ввод недоступен."})
            return
        if self._busy.is_set():
            return
        self._busy.set()

        def work() -> None:
            failed = False
            text = ""
            try:
                self._speaker.stop()
                self._emit("state", {"mode": "listening"})
                text = self._listener.listen_once()  # type: ignore[union-attr]
            except VoiceError as exc:
                self._emit("error", {"text": str(exc)})
                failed = True
            finally:
                self._busy.clear()
                self._emit("state", {"mode": "idle"})
            if text:
                self._emit("user", {"text": text})
                self.send_message(text)
            elif not failed:  # об ошибке микрофона уже сказали — «не расслышал» было бы вторым сообщением
                self._emit("error", {"text": "Не расслышал, повторите, пожалуйста."})

        self._run_async(work)

    def set_wake_listening(self, enabled: bool) -> dict[str, Any]:
        if self._listener is None:
            return {"ok": False, "text": self._voice_error or "Голосовой ввод недоступен."}
        if not to_bool(enabled, False):
            self._listener.stop_background()
            return {"ok": True, "text": "Фоновое прослушивание выключено."}
        wake = str(self._config.get("wake_word") or "джарвис")

        def on_command(command: str) -> None:
            if not command:
                self._emit("assistant", {"text": "Слушаю."})
                self._speaker.say("Слушаю.")
                self.listen_once()
                return
            self._emit("user", {"text": command})
            self.send_message(command)

        self._listener.start_background(
            wake,
            on_command,
            lambda err: self._emit("error", {"text": err}),
            lambda: self._busy.is_set() or self._speaker.is_speaking(),
        )
        return {"ok": True, "text": f"Слушаю фоном. Скажите «{wake.capitalize()}» и команду."}

    # ---------- закупки ----------
    def search_procurements(self, params: dict[str, Any]) -> None:
        params = clean_procurement(params)
        if params:
            self._config.update({"procurement": params})

        def work() -> None:
            try:
                self._toolbox.run_procurement_search()
            except ProcurementError as exc:
                self._emit("procurement_results", {"items": [], "analytics": None, "errors": [str(exc)], "notes": []})
            except Exception as exc:  # показываем на вкладке «Закупки», а не только в error.log
                _log_error(exc)
                self._emit("procurement_status", {"text": "", "busy": False})
                self._emit("procurement_results", {"items": [], "analytics": None, "errors": [_error_text(exc)], "notes": []})

        self._run_async(work)

    def export_procurements(self) -> dict[str, Any]:
        try:
            return {"ok": True, "text": f"Сохранено: {self._toolbox.export_results()}"}
        except (ProcurementError, ToolError, OSError) as exc:
            return {"ok": False, "text": str(exc)}

    def open_link(self, url: str) -> dict[str, Any]:
        parsed = urlparse(str(url or "").strip())
        host = (parsed.hostname or "").lower()
        if parsed.scheme in ("http", "https") and (host == "zakupki.gov.ru" or host.endswith(".zakupki.gov.ru")):
            import webbrowser

            webbrowser.open(parsed._replace(scheme="https").geturl())
            return {"ok": True}
        return {"ok": False, "text": "Ссылка не на zakupki.gov.ru."}

    # ---------- заявка (ТЗ, подбор, документы) ----------
    def _tender_job(self, job, *args) -> None:
        def work() -> None:
            try:
                job(*args)
            except (TenderError, ProcurementError, ValueError) as exc:
                self._emit("tender_status", {"text": str(exc), "busy": False, "error": True})
            except Exception as exc:  # LLMError и прочее — показываем пользователю
                from .llm import LLMError

                if not isinstance(exc, LLMError):
                    _log_error(exc)
                self._emit("tender_status", {"text": str(exc) or exc.__class__.__name__, "busy": False, "error": True})

        self._run_async(work)

    def tender_state(self) -> dict[str, Any]:
        return self._tender.snapshot()

    def tender_set_purchase(self, purchase: dict[str, Any], auto: bool = True) -> dict[str, Any]:
        """Выбор закупки. По умолчанию сразу запускается весь конвейер: документы → ТЗ → подбор → стоимость → Word."""
        try:
            self._tender.set_purchase(purchase if isinstance(purchase, dict) else {})
        except TenderError as exc:
            return {"ok": False, "text": str(exc)}
        if to_bool(auto, True):
            self.tender_run_all()
        return {"ok": True}

    def _tender_from_chat(self, number: str = "", remote: bool = False) -> str:
        """Команда из диалога или Telegram: полный разбор закупки в фоне на вкладке «Заявка»."""
        number = (number or "").strip()
        current = (self._tender.state.get("purchase") or {}).get("number")
        if not number and not current:
            raise ToolError("Назовите номер закупки или нажмите «В работу» у закупки на вкладке «Закупки».")

        def on_report(report: dict[str, Any]) -> None:
            self._analyst.upsert_report(report)
            if remote:
                self._notify_external("Разбор закупки готов", format_report(report), email=False)

        def job() -> None:
            client = self._toolbox.procurement_client()
            if number:
                self._emit("tender_status", {"text": "Ищу закупку в ЕИС…", "busy": True})
                self._tender.set_purchase(self._tender.find_purchase(number, client))
            self._tender.run_pipeline(client, on_report)

        self._emit("switch_tab", {"tab": "tender"})
        self._tender_job(job)
        target = f"№ {number}" if number else f"№ {current}"
        tail = " Пришлю отчёт сюда, когда закончу." if remote else " Ход работы — на вкладке «Заявка»."
        return f"Запустил полный разбор закупки {target}: документы, ТЗ, подбор, стоимость и документы заявки.{tail}"

    def tender_run_all(self) -> None:
        # клиент ЕИС создаётся внутри фоновой задачи: его ошибка попадёт в статус заявки, а не в JS
        self._tender_job(lambda: self._tender.run_pipeline(self._toolbox.procurement_client(), self._analyst.upsert_report))

    def tender_find(self, value: str) -> None:
        value = str(value or "").strip()
        if not value:
            self._emit("tender_status", {"text": "Введите номер закупки или ссылку на zakupki.gov.ru.", "busy": False, "error": True})
            return

        def job() -> None:
            self._emit("tender_status", {"text": "Ищу закупку в ЕИС…", "busy": True})
            purchase = self._tender.find_purchase(value, self._toolbox.procurement_client())
            self._tender.set_purchase(purchase)
            self._tender.run_pipeline(self._toolbox.procurement_client(), self._analyst.upsert_report)

        self._tender_job(job)

    def tender_fetch_docs(self) -> None:
        self._tender_job(lambda: self._tender.fetch_from_eis(self._toolbox.procurement_client()))

    def _pick_files(self, types: tuple[str, ...], multiple: bool = True) -> list[str]:
        if self._window is None:
            return []
        try:
            import webview
        except ImportError:
            self._emit("error", {"text": "Окно выбора файлов недоступно: не установлен pywebview."})
            return []

        mode = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or getattr(webview, "OPEN_DIALOG", 10)
        result = self._window.create_file_dialog(mode, allow_multiple=multiple, file_types=types)
        return list(result or [])

    def tender_pick_files(self) -> None:
        paths = self._pick_files((
            "Документы (*.docx;*.pdf;*.xlsx;*.rtf;*.txt;*.zip)",
            "Все файлы (*.*)",
        ))
        if paths:
            def job() -> None:
                self._tender.add_files(paths)
                self._tender.run_pipeline(self._toolbox.procurement_client(), self._analyst.upsert_report)

            self._tender_job(job)

    def tender_clear_files(self) -> None:
        self._tender_job(self._tender.clear_files)

    def tender_analyze(self) -> None:
        self._tender_job(self._tender.analyze)

    def tender_match(self) -> None:
        self._tender_job(self._tender.match)

    def tender_build_docs(self) -> None:
        self._tender_job(self._tender.build_documents)

    def tender_import_catalog(self) -> None:
        paths = self._pick_files(("Excel (*.xlsx;*.xlsm)",), multiple=False)
        if paths:
            self._tender_job(self._tender.import_catalog, paths[0])

    def tender_open(self, path: str = "") -> dict[str, Any]:
        """Открывает папку заявки или один из созданных в ней файлов."""
        import os

        folder = self._tender.folder().resolve()
        target = Path(str(path)).resolve() if path else folder
        if folder != target and folder not in target.parents:
            return {"ok": False, "text": "Можно открыть только файлы заявки."}
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
            return {"ok": True}
        except (AttributeError, OSError) as exc:
            return {"ok": False, "text": f"Не удалось открыть: {exc}"}

    def set_monitoring(self, enabled: bool) -> dict[str, Any]:
        with self._monitor_lock:  # двойной клик не должен запустить два цикла
            if self._monitor_stop is not None:
                self._monitor_stop.set()
                self._monitor_stop = None
            if not to_bool(enabled, False):
                return {"ok": True, "text": "Мониторинг выключен."}
            stop = threading.Event()
            self._monitor_stop = stop
        interval = max(10, to_int((self._config.get("procurement", {}) or {}).get("monitor_interval_min"), 30))

        def loop() -> None:
            first = self._toolbox.seen.is_empty()
            while not stop.is_set():
                try:
                    found, _ = self._toolbox.run_procurement_search()
                    new = self._toolbox.seen.filter_new(found)
                    if new and not first:
                        titles = "; ".join(p.title[:80] for p in new[:3])
                        text = f"Появилось новых закупок: {len(new)}. {titles}"
                        self._emit("monitor_new", {"count": len(new), "text": text})
                        self._speaker.say(f"{self._config.get('user_name', 'сэр').capitalize()}, появилось новых закупок: {len(new)}.")
                        if not self._config.get("analyst", {}).get("enabled"):
                            self._notify_external("Новые закупки", summary_text(new, limit=10), email=False)
                    first = False
                    if (self._config.get("analyst", {}) or {}).get("enabled") and found and not stop.is_set():
                        try:
                            self._analyst.run(found)
                        except Exception as exc:  # LLMError, ValueError и прочее — показываем на вкладке «Аналитика»
                            if not self._analyst_already_running(exc):
                                if not isinstance(exc, ValueError):
                                    _log_error(exc)
                                self._emit("analyst_status", {"text": str(exc) or exc.__class__.__name__,
                                                              "busy": False, "error": True})
                except ProcurementError as exc:
                    self._emit("error", {"text": f"Мониторинг: {exc}"})
                except Exception as exc:  # не молчим: иначе мониторинг «работает», но ничего не находит
                    _log_error(exc)
                    self._emit("error", {"text": f"Мониторинг: {_error_text(exc)}"})
                stop.wait(interval * 60)

        threading.Thread(target=loop, daemon=True, name="jarvis-monitor").start()
        return {"ok": True, "text": f"Мониторинг включён: проверка каждые {interval} мин."}

    # ---------- уведомления, Telegram, почта ----------
    def _notify_external(self, title: str, text: str, email: bool = True) -> None:
        try:
            self._notifier.notify(title, text, email=email)
        except Exception as exc:  # уведомление не должно ломать мониторинг
            _log_error(exc)

    def _on_telegram_message(self, text: str) -> str:
        text = str(text or "").strip()
        if text.startswith("/zakupki"):
            try:
                found, errors = self._toolbox.run_procurement_search()
            except ProcurementError as exc:
                return f"Поиск не удался: {exc}"
            except Exception as exc:
                _log_error(exc)
                return f"Поиск не удался: {_error_text(exc)}"
            return summary_text(found, limit=10) + ("\n" + "; ".join(errors) if errors else "")
        if text.startswith("/analytics"):
            reports = self._analyst.store.reports[:3]
            if not reports:
                return "Отчётов аналитики пока нет. Включите автоаналитику на вкладке «Аналитика» в окне Джарвиса."
            return "\n\n———\n\n".join(format_report(r) for r in reports)
        self._emit("user", {"text": f"[Telegram] {text}"})
        answer = self._remote_brain.ask(text)
        self._emit("assistant", {"text": f"[Telegram] {answer}"})
        return answer

    def telegram_status(self) -> dict[str, Any]:
        return self._telegram.status()

    def telegram_connect(self, token: str) -> dict[str, Any]:
        try:
            info = self._telegram.connect(str(token or ""))
        except TelegramError as exc:
            return {"ok": False, "text": str(exc)}
        bot = f"@{info['bot']}" if info.get("bot") else "вашему боту"
        if not info.get("code"):
            name = f" @{info['bot']}" if info.get("bot") else ""
            return {"ok": True, "code": "", "text": f"Бот{name} подключён, чат уже привязан."}
        return {"ok": True, "code": info["code"], "text": f"Отправьте {bot} сообщение: /start {info['code']}"}

    def telegram_open_bot(self) -> dict[str, Any]:
        """Открывает бота в Telegram по ссылке с кодом привязки: «Старт» сам отправит код."""
        import webbrowser

        st = self._telegram.status()
        if not st["bot"]:
            return {"ok": False, "text": "Сначала вставьте токен и нажмите «Подключить»."}
        link = st["link"] or f"https://t.me/{st['bot']}"
        webbrowser.open(link)
        return {"ok": True, "text": "Откроется Telegram — нажмите в чате с ботом «Старт» (или «Запустить»)."}

    def telegram_disconnect(self) -> dict[str, Any]:
        self._telegram.disconnect()
        return {"ok": True, "text": "Telegram отключён."}

    def telegram_test(self) -> dict[str, Any]:
        if not self._telegram.chat_id():
            return {"ok": False, "text": "Сначала привяжите чат: /start и код."}
        ok = self._telegram.send("Проверка связи. Джарвис на линии.")
        return {"ok": ok, "text": "Сообщение отправлено." if ok else "Не удалось отправить."}

    def email_save(self, values: dict[str, Any]) -> dict[str, Any]:
        values = values if isinstance(values, dict) else {}
        preset = str(values.get("preset") or "custom").strip().lower()
        if preset in SMTP_PRESETS:
            host, port = SMTP_PRESETS[preset]
        else:
            preset = "custom"
            host = str(values.get("host") or "").strip()
            port = to_int(values.get("port"), 465)
            if not 0 < port < 65536:
                port = 465
            if not host:
                return {"ok": False, "text": "Укажите SMTP-сервер, например smtp.yandex.ru."}
        login = str(values.get("login") or "").strip()
        self._config.update({"email": {
            "enabled": to_bool(values.get("enabled"), True), "preset": preset, "host": host, "port": port,
            "login": login, "to": str(values.get("to") or "").strip() or login,
            "notify": to_bool(values.get("notify"), True),
        }})
        password = str(values.get("password", ""))
        if password and not set_secret(SMTP_KEY, password):
            return {"ok": False, "text": "Не удалось сохранить пароль в диспетчере учётных данных Windows."}
        return {"ok": True, "text": "Почта сохранена.", "has_password": bool(get_secret(SMTP_KEY))}

    def email_test(self) -> None:
        def work() -> None:
            try:
                self._mailer.send("J.A.R.V.I.S.: проверка почты", "Если вы читаете это письмо, отправка отчётов работает.")
                self._emit("toast", {"text": "Тестовое письмо отправлено."})
            except OSError as exc:
                self._emit("error", {"text": str(exc)})

        self._run_async(work)

    def email_status(self) -> dict[str, Any]:
        return {"has_password": bool(get_secret(SMTP_KEY)), "configured": self._mailer.configured()}

    # ---------- аналитика ----------
    def analyst_state(self) -> dict[str, Any]:
        return self._analyst.snapshot()

    def analyst_save(self, values: dict[str, Any]) -> dict[str, Any]:
        values = values if isinstance(values, dict) else {}
        clean = {
            "enabled": to_bool(values.get("enabled"), False),
            "profile": str(values.get("profile") or "").strip()[:3000],
            "min_score": max(0, min(100, to_int(values.get("min_score"), 60))),
            "top_n": max(1, min(10, to_int(values.get("top_n"), 3))),
        }
        self._config.update({"analyst": clean})
        if clean["enabled"]:
            self.set_monitoring(True)
        return {"ok": True, "text": "Автоаналитика включена: Джарвис будет проверять закупки по расписанию мониторинга."
                if clean["enabled"] else "Настройки аналитики сохранены."}

    def _analyst_running(self) -> bool:
        lock = getattr(self._analyst, "_lock", None)
        return bool(lock is not None and lock.locked())

    def _analyst_already_running(self, exc: BaseException) -> bool:
        """«Аналитика уже выполняется» — не ошибка: показываем тостом и не трогаем кнопку (идёт прогон)."""
        if isinstance(exc, ValueError) and "уже выполняется" in str(exc):
            self._emit("toast", {"text": str(exc)})
            return True
        return False

    def analyst_run_now(self) -> None:
        if self._analyst_running():
            self._emit("toast", {"text": "Аналитика уже выполняется."})
            return

        def work() -> None:
            try:
                self._emit("analyst_status", {"text": "Ищу закупки по вашим тегам…", "busy": True})
                found, _ = self._toolbox.run_procurement_search()
                self._analyst.run(found)
            except (ProcurementError, ValueError) as exc:
                if not self._analyst_already_running(exc):
                    self._emit("analyst_status", {"text": str(exc), "busy": False, "error": True})
            except Exception as exc:
                from .llm import LLMError

                if not isinstance(exc, LLMError):
                    _log_error(exc)
                self._emit("analyst_status", {"text": str(exc) or exc.__class__.__name__, "busy": False, "error": True})

        self._run_async(work)

    def analyst_to_tender(self, number: str) -> dict[str, Any]:
        number = str(number or "").strip()
        for report in self._analyst.store.reports:
            purchase = report.get("purchase") or {} if isinstance(report, dict) else {}
            if str(purchase.get("number") or "") == number and number:
                return self.tender_set_purchase(purchase)
        return {"ok": False, "text": "Отчёт не найден."}

    # ---------- ярлык ----------
    def create_shortcut(self) -> dict[str, Any]:
        from .shortcut import create_desktop_shortcut

        try:
            path = create_desktop_shortcut()
            self._config.update({"shortcut_created": True})
            return {"ok": True, "text": f"Ярлык создан: {path}"}
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "text": f"Не удалось создать ярлык: {exc}"}

    def ensure_shortcut(self, *_args) -> None:
        """Один раз при первом запуске создаёт ярлык на рабочем столе (вызывается на событии loaded окна)."""
        # loaded приходит при каждой загрузке страницы — не запускаем вторую попытку параллельно и повторно
        if self._shortcut_started or self._config.get("shortcut_created"):
            return
        self._shortcut_started = True

        def work() -> None:
            res = self.create_shortcut()
            if res.get("ok"):
                self._emit("toast", {"text": "На рабочем столе создан ярлык J.A.R.V.I.S."})

        self._run_async(work)

    def shutdown(self, *_args) -> None:
        steps = [
            self._telegram.stop,
            lambda: self._monitor_stop.set() if self._monitor_stop is not None else None,
            lambda: self._listener.stop_background() if self._listener is not None else None,
            self._speaker.stop,
            self._toolbox.reminders.cancel_all,
        ]
        for step in steps:  # одна ошибка не должна мешать остановить остальное
            try:
                step()
            except Exception as exc:
                _log_error(exc)
