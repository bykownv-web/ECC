"""Мост между окном (HTML/JS) и Python. Методы класса JarvisApi вызываются из JS через pywebview."""

from __future__ import annotations

import json
import threading
import traceback
from typing import Any
from urllib.parse import urlparse

from . import __version__
from .brain import Brain
from .config import Config, app_data_dir, get_api_key, set_api_key
from .procurement import ProcurementError
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

SETTINGS_KEYS = {"assistant_name", "user_name", "model", "effort", "voice_enabled", "wake_word", "city", "apps"}
PROCUREMENT_KEYS = {"tags", "fz44", "fz223", "only_active", "price_from", "price_to", "pages_per_tag",
                    "monitor_interval_min", "ca_bundle"}


def _log_error(exc: BaseException) -> None:
    try:
        with open(app_data_dir() / "error.log", "a", encoding="utf-8") as fh:
            fh.write("".join(traceback.format_exception(exc)) + "\n")
    except OSError:
        pass


class JarvisApi:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._window = None
        self._toolbox = ToolBox(config, self._emit)
        self._brain = Brain(config, self._toolbox, on_tool=self._on_tool)
        self._speaker = Speaker()
        self._speaker.enabled = bool(config.get("voice_enabled", True))
        self._busy = threading.Event()
        self._monitor_stop: threading.Event | None = None
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
            "has_key": bool(get_api_key()),
            "voice_available": self._listener is not None,
            "voice_error": self._voice_error,
            "version": __version__,
            "greeting": self._greeting(),
        }

    def _greeting(self) -> str:
        user = self._config.get("user_name", "сэр")
        if get_api_key():
            return f"Системы в норме. Чем могу помочь, {user}?"
        return f"Здравствуйте, {user}. Работаю в простом режиме — добавьте ключ Claude в настройках."

    def save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        values = values or {}
        clean: dict[str, Any] = {k: v for k, v in values.items() if k in SETTINGS_KEYS}
        if isinstance(clean.get("apps"), dict):
            clean["apps"] = {
                str(k).strip().lower(): str(v).strip()
                for k, v in clean["apps"].items()
                if str(k).strip() and str(v).strip()
            }
        else:
            clean.pop("apps", None)
        proc = values.get("procurement")
        if isinstance(proc, dict):
            clean["procurement"] = {k: v for k, v in proc.items() if k in PROCUREMENT_KEYS}
        if "apps" in clean:
            # Список программ заменяется целиком, чтобы удалённые пункты не возвращались.
            self._config.set("apps", clean.pop("apps"))
        self._config.update(clean)
        self._speaker.enabled = bool(self._config.get("voice_enabled", True))
        return self.get_state()

    def save_api_key(self, key: str) -> dict[str, Any]:
        key = (key or "").strip()
        if key and not key.startswith("sk-ant-"):
            return {"ok": False, "text": "Ключ Claude обычно начинается с sk-ant-."}
        ok = set_api_key(key)
        if not ok:
            return {"ok": False, "text": "Не удалось сохранить ключ в диспетчере учётных данных Windows."}
        self._brain.reset()
        return {"ok": True, "text": "Ключ сохранён." if key else "Ключ удалён."}

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

    def open_link(self, url: str) -> None:
        parsed = urlparse(url or "")
        if parsed.scheme == "https" and parsed.netloc.endswith("zakupki.gov.ru"):
            import webbrowser

            webbrowser.open(parsed.geturl())

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
                    first = False
                except ProcurementError as exc:
                    self._emit("error", {"text": f"Мониторинг: {exc}"})
                except Exception as exc:
                    _log_error(exc)
                stop.wait(interval * 60)

        threading.Thread(target=loop, daemon=True, name="jarvis-monitor").start()
        return {"ok": True, "text": f"Мониторинг включён: проверка каждые {interval} мин."}

    def shutdown(self) -> None:
        if self._monitor_stop is not None:
            self._monitor_stop.set()
        if self._listener is not None:
            self._listener.stop_background()
        self._speaker.stop()
        self._toolbox.reminders.cancel_all()
