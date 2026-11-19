"""Мост между окном (HTML/JS) и Python. Методы класса JarvisApi вызываются из JS через pywebview."""

from __future__ import annotations

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
from .tools import ToolBox, ToolError
from .voice import Listener, Speaker, VoiceError

TOOL_LABELS = {
    "search_procurements": "ищу закупки в ЕИС",
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
        return getattr(self._toolbox, item)


class JarvisApi:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._window = None
        self._toolbox = ToolBox(config, self._emit)
        self._tender = TenderWorkspace(config, self._emit)
        self._brain = Brain(config, self._toolbox, on_tool=self._on_tool)
        self._speaker = Speaker()
        self._speaker.eleven_key = get_elevenlabs_key
        self._speaker.on_error = lambda text: self._emit("error", {"text": text})
        self._apply_voice_settings()
        self._busy = threading.Event()
        self._monitor_stop: threading.Event | None = None
        self._remote_brain = Brain(config, RemoteToolBox(self._toolbox))  # Telegram — отдельная история диалога
        self._telegram = TelegramBot(config, self._on_telegram_message, self._emit)
        self._mailer = Mailer(config)
        self._notifier = Notifier(self._telegram, self._mailer, config)
        self._analyst = Analyst(config, self._toolbox.procurement_client, self._emit, notify=self._notify_external)
        self._toolbox.tender_runner = self._tender_from_chat
        self._telegram.start()
        if config.get("analyst", {}).get("enabled"):
            self.set_monitoring(True)
        try:
            self._listener: Listener | None = Listener()
            self._voice_error = ""
        except VoiceError as exc:
            self._listener = None
            self._voice_error = str(exc)

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
            except Exception as exc:  # фоновые ошибки не должны ронять окно
                _log_error(exc)
                self._emit("error", {"text": f"Ошибка: {exc.__class__.__name__}. Подробности в error.log."})

        threading.Thread(target=wrapper, daemon=True).start()

    # ---------- состояние и настройки ----------
    def get_state(self) -> dict[str, Any]:
        cfg = self._config.as_dict()
        return {
            "config": cfg,
            "ai_label": self._ai_label(),
            "ai_ok": self._brain.has_ai(),
            "has_key": bool(get_api_key()),
            "has_eleven_key": bool(get_elevenlabs_key()),
            "has_openai_key": bool(get_secret("openai-compatible-api-key")),
            "has_gigachat_key": bool(get_secret("gigachat-auth-key")),
            "default_minus_words": DEFAULT_MINUS_WORDS,
            "telegram": self._telegram.status(),
            "email": self.email_status(),
            "voice_available": self._listener is not None,
            "voice_error": self._voice_error,
            "version": __version__,
            "greeting": self._greeting(),
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
        values = values or {}
        clean: dict[str, Any] = {k: v for k, v in values.items() if k in SETTINGS_KEYS}
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
            clean["company"] = {k: str(v).strip()[:300] for k, v in company.items() if k in COMPANY_KEYS}
        proc = values.get("procurement")
        if isinstance(proc, dict):
            clean["procurement"] = {k: v for k, v in proc.items() if k in PROCUREMENT_KEYS}
        if "apps" in clean:
            # Список программ заменяется целиком, чтобы удалённые пункты не возвращались.
            self._config.set("apps", clean.pop("apps"))
        self._config.update(clean)
        self._apply_voice_settings()
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
            try:
                self._speaker.stop()
                self._emit("state", {"mode": "listening"})
                text = self._listener.listen_once()  # type: ignore[union-attr]
            except VoiceError as exc:
                self._emit("error", {"text": str(exc)})
                text = ""
            finally:
                self._busy.clear()
                self._emit("state", {"mode": "idle"})
            if text:
                self._emit("user", {"text": text})
                self.send_message(text)
            else:
                self._emit("error", {"text": "Не расслышал, повторите, пожалуйста."})

        self._run_async(work)

    def set_wake_listening(self, enabled: bool) -> dict[str, Any]:
        if self._listener is None:
            return {"ok": False, "text": self._voice_error or "Голосовой ввод недоступен."}
        if not enabled:
            self._listener.stop_background()
            return {"ok": True, "text": "Фоновое прослушивание выключено."}
        wake = self._config.get("wake_word", "джарвис")

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
        params = {k: v for k, v in (params or {}).items() if k in PROCUREMENT_KEYS}
        if params:
            self._config.update({"procurement": params})

        def work() -> None:
            try:
                self._toolbox.run_procurement_search()
            except ProcurementError as exc:
                self._emit("procurement_results", {"items": [], "analytics": None, "errors": [str(exc)]})

        self._run_async(work)

    def export_procurements(self) -> dict[str, Any]:
        try:
            return {"ok": True, "text": f"Сохранено: {self._toolbox.export_results()}"}
        except (ProcurementError, ToolError, OSError) as exc:
            return {"ok": False, "text": str(exc)}

    def open_link(self, url: str) -> dict[str, Any]:
        parsed = urlparse(url or "")
        if parsed.scheme in ("http", "https") and parsed.netloc.endswith("zakupki.gov.ru"):
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
            self._tender.set_purchase(purchase or {})
        except TenderError as exc:
            return {"ok": False, "text": str(exc)}
        if auto:
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
        self._tender_job(self._tender.run_pipeline, self._toolbox.procurement_client(), self._analyst.upsert_report)

    def tender_find(self, value: str) -> None:
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
        import webview

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
        target = Path(path).resolve() if path else folder
        if folder != target and folder not in target.parents:
            return {"ok": False, "text": "Можно открыть только файлы заявки."}
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
            return {"ok": True}
        except (AttributeError, OSError) as exc:
            return {"ok": False, "text": f"Не удалось открыть: {exc}"}

    def set_monitoring(self, enabled: bool) -> dict[str, Any]:
        if self._monitor_stop is not None:
            self._monitor_stop.set()
            self._monitor_stop = None
        if not enabled:
            return {"ok": True, "text": "Мониторинг выключен."}
        stop = threading.Event()
        self._monitor_stop = stop
        interval = max(10, int(self._config.get("procurement", {}).get("monitor_interval_min", 30) or 30))

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
                    if self._config.get("analyst", {}).get("enabled") and found:
                        try:
                            self._analyst.run(found)
                        except ValueError as exc:
                            self._emit("analyst_status", {"text": str(exc), "busy": False, "error": True})
                except ProcurementError as exc:
                    self._emit("error", {"text": f"Мониторинг: {exc}"})
                except Exception as exc:
                    _log_error(exc)
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
        if text.startswith("/zakupki"):
            try:
                found, errors = self._toolbox.run_procurement_search()
            except ProcurementError as exc:
                return f"Поиск не удался: {exc}"
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
            info = self._telegram.connect(token)
        except TelegramError as exc:
            return {"ok": False, "text": str(exc)}
        bot = f"@{info['bot']}" if info.get("bot") else "вашему боту"
        return {"ok": True, "code": info["code"], "text": f"Отправьте {bot} сообщение: /start {info['code']}"}

    def telegram_disconnect(self) -> dict[str, Any]:
        self._telegram.disconnect()
        return {"ok": True, "text": "Telegram отключён."}

    def telegram_test(self) -> dict[str, Any]:
        if not self._telegram.chat_id():
            return {"ok": False, "text": "Сначала привяжите чат: /start и код."}
        ok = self._telegram.send("Проверка связи. Джарвис на линии.")
        return {"ok": ok, "text": "Сообщение отправлено." if ok else "Не удалось отправить."}

    def email_save(self, values: dict[str, Any]) -> dict[str, Any]:
        values = values or {}
        preset = str(values.get("preset", "custom"))
        host, port = SMTP_PRESETS.get(preset, (str(values.get("host", "")).strip(), values.get("port") or 465))
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 465
        self._config.update({"email": {
            "enabled": bool(values.get("enabled", True)), "preset": preset, "host": host, "port": port,
            "login": str(values.get("login", "")).strip(), "to": str(values.get("to", "")).strip() or str(values.get("login", "")).strip(),
            "notify": bool(values.get("notify", True)),
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
        values = values or {}
        clean = {
            "enabled": bool(values.get("enabled")),
            "profile": str(values.get("profile", ""))[:3000],
            "min_score": max(0, min(100, int(values.get("min_score") or 60))),
            "top_n": max(1, min(10, int(values.get("top_n") or 3))),
        }
        self._config.update({"analyst": clean})
        if clean["enabled"]:
            self.set_monitoring(True)
        return {"ok": True, "text": "Автоаналитика включена: Джарвис будет проверять закупки по расписанию мониторинга."
                if clean["enabled"] else "Настройки аналитики сохранены."}

    def analyst_run_now(self) -> None:
        def work() -> None:
            try:
                self._emit("analyst_status", {"text": "Ищу закупки по вашим тегам…", "busy": True})
                found, _ = self._toolbox.run_procurement_search()
                self._analyst.run(found)
            except (ProcurementError, ValueError) as exc:
                self._emit("analyst_status", {"text": str(exc), "busy": False, "error": True})
            except Exception as exc:
                from .llm import LLMError

                if not isinstance(exc, LLMError):
                    _log_error(exc)
                self._emit("analyst_status", {"text": str(exc) or exc.__class__.__name__, "busy": False, "error": True})

        self._run_async(work)

    def analyst_to_tender(self, number: str) -> dict[str, Any]:
        for report in self._analyst.store.reports:
            if report["purchase"].get("number") == number:
                return self.tender_set_purchase(report["purchase"])
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

    def ensure_shortcut(self) -> None:
        """Один раз при первом запуске создаёт ярлык на рабочем столе."""
        if self._config.get("shortcut_created"):
            return

        def work() -> None:
            res = self.create_shortcut()
            if res["ok"]:
                self._emit("toast", {"text": "На рабочем столе создан ярлык J.A.R.V.I.S."})

        self._run_async(work)

    def shutdown(self) -> None:
        self._telegram.stop()
        if self._monitor_stop is not None:
            self._monitor_stop.set()
        if self._listener is not None:
            self._listener.stop_background()
        self._speaker.stop()
        self._toolbox.reminders.cancel_all()
