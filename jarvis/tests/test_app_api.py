"""Проверка моста JarvisApi без pywebview, микрофона и сети.

Окно подменяется объектом, который записывает evaluate_js; Listener — заглушкой; поиск в ЕИС — фейком.
"""

from __future__ import annotations

import inspect
import json
import threading
import time

import pytest

from jarvis_app import api as api_mod
from jarvis_app import brain as brain_mod
from jarvis_app import notify
from jarvis_app import tools as tools_mod
from jarvis_app.config import Config
from jarvis_app.procurement import ProcurementError, Purchase
from jarvis_app.relevance import DEFAULT_MINUS_WORDS
from jarvis_app.voice import VoiceError


class FakeWindow:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def evaluate_js(self, code: str) -> None:
        prefix = "window.jarvis && window.jarvis.onEvent("
        assert code.startswith(prefix) and code.endswith(")")
        data = json.loads(code[len(prefix):-1])
        with self.lock:
            self.events.append((data["event"], data["payload"]))

    def named(self, name):
        with self.lock:
            return [p for e, p in self.events if e == name]

    def create_file_dialog(self, *a, **kw):
        return None


class FakeListener:
    def __init__(self):
        self.result: str | Exception = ""
        self.background = None

    def listen_once(self, *a, **kw):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def start_background(self, *a):
        self.background = a

    def stop_background(self):
        self.background = None


class NoNetwork:
    """Telegram в тестах недоступен: любой запрос — ошибка соединения, а не настоящая сеть."""

    def post(self, *a, **kw):
        import requests

        raise requests.ConnectionError("нет сети в тестах")


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


@pytest.fixture
def secrets(monkeypatch):
    store: dict[str, str] = {}
    get = lambda name: store.get(name, "")  # noqa: E731

    def put(name, value):
        if value:
            store[name] = value
        else:
            store.pop(name, None)
        return True

    for mod in (notify, api_mod, brain_mod):
        monkeypatch.setattr(mod, "get_secret", get, raising=False)
        monkeypatch.setattr(mod, "set_secret", put, raising=False)
    monkeypatch.setattr(api_mod, "get_api_key", lambda: "")
    monkeypatch.setattr(brain_mod, "get_api_key", lambda: "")
    monkeypatch.setattr(api_mod, "get_elevenlabs_key", lambda: "")
    return store


@pytest.fixture
def make_api(tmp_path, monkeypatch, secrets):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setattr(api_mod, "Listener", FakeListener)
    searches = []

    def fake_search(self, overrides=None):
        searches.append(overrides)
        found = [Purchase(number="0101", url="https://zakupki.gov.ru/x", title="Поставка проектора", price=100.0)]
        self._emit("procurement_results", {"items": [p.to_dict() for p in found], "analytics": {}, "errors": [], "notes": []})
        return found, []

    monkeypatch.setattr(tools_mod.ToolBox, "run_procurement_search", fake_search)
    created = []

    def factory(values=None):
        config = Config(tmp_path / "config.json")
        config.update({"ai_provider": "none", "voice_enabled": False, **(values or {})})
        a = api_mod.JarvisApi(config)
        a._telegram._http = NoNetwork()
        window = FakeWindow()
        a._attach(window)
        created.append(a)
        return a, window, config, searches

    yield factory
    for a in created:
        a.shutdown()


def serialisable(value) -> bool:
    json.dumps(value)
    return True


# ---------- конструктор ----------

def test_init_with_analyst_enabled_starts_monitoring_after_all_fields(make_api):
    a, _, _, searches = make_api({"analyst": {"enabled": True, "profile": ""}})
    assert a._monitor_stop is not None
    assert a._listener is not None and a._voice_error == ""
    assert wait_for(lambda: len(searches) >= 1)


def test_init_survives_broken_listener(make_api, monkeypatch):
    def broken():
        raise AttributeError("PyAudio сломан")

    monkeypatch.setattr(api_mod, "Listener", broken)
    a, _, _, _ = make_api()
    assert a._listener is None and "AttributeError" in a._voice_error
    assert a.listen_once() is None
    assert a.set_wake_listening(True)["ok"] is False


# ---------- все публичные методы с хорошими и плохими данными ----------

CALLS = [
    ("get_state", ()), ("get_system_stats", ()), ("save_ai_key", ("openai", "k")), ("save_ai_key", ("bogus", "k")),
    ("save_ai_key", (None, None)), ("test_ai", ()), ("check_ollama", ()), ("save_settings", ({"city": "Казань"},)),
    ("save_settings", (None,)), ("save_settings", ([1, 2],)), ("save_settings", ({"company": {"name": None}},)),
    ("test_voice", ()), ("save_api_key", ("bad",)), ("save_api_key", (None,)), ("save_elevenlabs_key", ("x" * 30,)),
    ("save_elevenlabs_key", (12345,)), ("send_message", ("",)), ("send_message", (None,)), ("reset_chat", ()),
    ("stop_speaking", ()), ("set_wake_listening", (False,)), ("search_procurements", ({"tags": ["проектор"]},)),
    ("search_procurements", (None,)), ("search_procurements", ("мусор",)), ("export_procurements", ()),
    ("open_link", ("javascript:alert(1)",)), ("open_link", (None,)), ("open_link", ("https://zakupki.gov.ru.evil.com/x",)),
    ("tender_state", ()), ("tender_set_purchase", ({},)), ("tender_set_purchase", (None,)), ("tender_set_purchase", ("x",)),
    ("tender_find", ("",)), ("tender_pick_files", ()), ("tender_import_catalog", ()), ("tender_open", ("/etc/passwd",)),
    ("tender_open", (None,)), ("set_monitoring", (False,)), ("telegram_status", ()), ("telegram_connect", ("",)),
    ("telegram_connect", (None,)), ("telegram_connect", ("без двоеточия",)), ("telegram_open_bot", ()),
    ("telegram_disconnect", ()), ("telegram_test", ()), ("email_save", ({"preset": "yandex", "login": "a@ya.ru"},)),
    ("email_save", (None,)), ("email_save", ({"preset": "custom", "host": "", "port": "abc"},)), ("email_test", ()),
    ("email_status", ()), ("analyst_state", ()), ("analyst_save", ({"min_score": "abc", "top_n": None, "profile": None},)),
    ("analyst_save", (None,)), ("analyst_to_tender", ("нет",)), ("analyst_to_tender", (None,)), ("create_shortcut", ()),
]


@pytest.mark.parametrize("name,args", CALLS, ids=[f"{n}-{i}" for i, (n, _) in enumerate(CALLS)])
def test_every_js_method_returns_json_and_never_raises(make_api, name, args):
    a, _, _, _ = make_api()
    result = getattr(a, name)(*args)
    assert serialisable(result)
    if isinstance(result, dict) and "ok" in result and not result["ok"]:
        assert isinstance(result.get("text"), str) and result["text"]


def test_all_public_methods_are_guarded_and_keep_signature():
    for name, fn in vars(api_mod.JarvisApi).items():
        if name.startswith("_") or not callable(fn):
            continue
        assert hasattr(fn, "__wrapped__"), name
        # pywebview читает имена параметров через getfullargspec
        assert inspect.getfullargspec(fn).args == inspect.getfullargspec(fn.__wrapped__).args, name


def test_unexpected_exception_in_js_method_becomes_error_dict(make_api, monkeypatch):
    a, window, _, _ = make_api()
    monkeypatch.setattr(a._tender, "snapshot", lambda: (_ for _ in ()).throw(TypeError("сломалось")))
    res = a.tender_state()
    assert res == {"ok": False, "text": "Ошибка: сломалось"}
    monkeypatch.setattr(a._speaker, "say", lambda text: (_ for _ in ()).throw(RuntimeError("нет звука")))
    assert a.test_voice() is None
    assert any("нет звука" in p["text"] for p in window.named("error"))


def test_non_json_results_are_converted(make_api, monkeypatch):
    from pathlib import Path

    a, _, _, _ = make_api()
    monkeypatch.setattr(a._tender, "snapshot", lambda: {"folder": Path("/tmp/x"), "tags": {"a"}})
    res = a.tender_state()
    assert res["folder"] == str(Path("/tmp/x"))


# ---------- фоновые задачи сообщают об ошибках ----------

def test_run_async_surfaces_error_text(make_api):
    a, window, _, _ = make_api()

    def boom():
        raise KeyError("purchase")

    a._run_async(boom)
    assert wait_for(lambda: window.named("error"))
    assert "purchase" in window.named("error")[0]["text"]


def test_send_message_error_in_brain_reaches_ui_and_clears_busy(make_api, monkeypatch):
    a, window, _, _ = make_api()
    monkeypatch.setattr(a._brain, "ask", lambda text: (_ for _ in ()).throw(TypeError("bad")))
    a.send_message("привет")
    assert wait_for(lambda: window.named("error") and not a._busy.is_set())
    assert window.named("state")[-1] == {"mode": "idle"}


def test_search_procurements_unexpected_error_goes_to_table(make_api, monkeypatch):
    a, window, _, _ = make_api()
    monkeypatch.setattr(a._toolbox, "run_procurement_search", lambda: (_ for _ in ()).throw(TypeError("x")))
    a.search_procurements({"tags": ["проектор"]})
    assert wait_for(lambda: window.named("procurement_results"))
    assert window.named("procurement_results")[-1]["errors"] == ["Ошибка: x"]


def test_tender_run_all_client_error_is_shown_in_tender_status(make_api, monkeypatch):
    a, window, _, _ = make_api()
    monkeypatch.setattr(a._toolbox, "procurement_client", lambda: (_ for _ in ()).throw(ProcurementError("нет сертификата")))
    a.tender_run_all()  # раньше клиент создавался до фоновой задачи и ошибка улетала в JS
    assert wait_for(lambda: window.named("tender_status"))
    assert window.named("tender_status")[-1]["error"] is True


def test_tender_find_requires_value(make_api):
    a, window, _, _ = make_api()
    a.tender_find("  ")
    assert window.named("tender_status")[-1]["error"] is True


def test_listen_once_voice_error_reported_once(make_api):
    a, window, _, _ = make_api()
    a._listener.result = VoiceError("Микрофон не найден")
    a.listen_once()
    assert wait_for(lambda: not a._busy.is_set() and window.named("state"))
    time.sleep(0.05)
    texts = [p["text"] for p in window.named("error")]
    assert texts == ["Микрофон не найден"]


# ---------- мониторинг ----------

def test_set_monitoring_restart_stops_previous_loop(make_api):
    a, _, _, searches = make_api()
    a.set_monitoring(True)
    first = a._monitor_stop
    a.set_monitoring(True)
    assert first.is_set() and a._monitor_stop is not first and not a._monitor_stop.is_set()
    assert a.set_monitoring(False)["text"] == "Мониторинг выключен."
    assert a._monitor_stop is None


def test_set_monitoring_bad_interval_does_not_raise(make_api):
    a, _, config, _ = make_api()
    config.update({"procurement": {"monitor_interval_min": "abc"}})
    res = a.set_monitoring(True)
    assert res["ok"] and "30" in res["text"]


def test_monitor_loop_surfaces_unexpected_errors(make_api, monkeypatch):
    a, window, _, _ = make_api()
    monkeypatch.setattr(a._toolbox, "run_procurement_search", lambda: (_ for _ in ()).throw(TypeError("сломан парсер")))
    a.set_monitoring(True)
    assert wait_for(lambda: any("сломан парсер" in p["text"] for p in window.named("error")))


def test_monitor_loop_analyst_llm_error_is_shown(make_api, monkeypatch):
    from jarvis_app.llm import LLMError

    a, window, _, _ = make_api({"analyst": {"enabled": False, "profile": "проекторы"}})
    a._config.update({"analyst": {"enabled": True}})
    monkeypatch.setattr(a._analyst, "run", lambda found: (_ for _ in ()).throw(LLMError("модель недоступна")))
    a.set_monitoring(True)
    assert wait_for(lambda: window.named("analyst_status"))
    assert window.named("analyst_status")[-1] == {"text": "модель недоступна", "busy": False, "error": True}


# ---------- настройки ----------

def test_save_settings_merging(make_api):
    a, _, config, _ = make_api()
    a.save_settings({"apps": {"Блокнот ": "notepad.exe", "": "x", "пусто": " "}, "voice_enabled": "false",
                     "company": {"name": " ООО Ромашка ", "inn": None, "evil": "x"},
                     "procurement": {"tags": "проектор, экран", "strict": "false", "minus_words": None,
                                     "price_from": "100 000", "pages_per_tag": "abc", "unknown": 1},
                     "wake_word": "", "ai_provider": "skynet", "ollama_url": "file:///etc"})
    assert config.get("apps") == {"блокнот": "notepad.exe"}  # заменён целиком
    assert config.get("voice_enabled") is False
    company = config.get("company")
    assert company["name"] == "ООО Ромашка" and company["inn"] == "" and "evil" not in company
    assert company["director_position"] == "Генеральный директор"  # остальные поля сохранились
    proc = config.get("procurement")
    assert proc["tags"] == ["проектор", "экран"] and proc["strict"] is False
    assert proc["minus_words"] is None and proc["price_from"] == 100000 and proc["pages_per_tag"] == 2
    assert "unknown" not in proc and proc["fz44"] is True
    assert config.get("wake_word") == "джарвис" and config.get("ai_provider") == "none"
    assert config.get("ollama_url").startswith("http")
    # список минус-слов заменяется, а не склеивается
    a.save_settings({"procurement": {"minus_words": ["медицин", "дорог"]}})
    a.save_settings({"procurement": {"minus_words": ["шин"]}})
    assert config.get("procurement")["minus_words"] == ["шин"]


def test_save_settings_interval_change_restarts_monitor(make_api):
    a, _, _, _ = make_api()
    a.set_monitoring(True)
    old = a._monitor_stop
    a.save_settings({"procurement": {"monitor_interval_min": 45}})
    assert old.is_set() and a._monitor_stop is not None and a._monitor_stop is not old


def test_email_save_presets(make_api, secrets):
    a, _, config, _ = make_api()
    res = a.email_save({"preset": "yandex", "host": "evil", "port": 25, "login": "me@ya.ru", "password": "p"})
    assert res["ok"] and res["has_password"]
    email = config.get("email")
    assert (email["host"], email["port"], email["to"]) == ("smtp.yandex.ru", 465, "me@ya.ru")
    a.email_save({"preset": "custom", "host": "smtp.corp.ru", "port": "587", "login": "a@corp.ru", "to": "b@corp.ru"})
    email = config.get("email")
    assert (email["preset"], email["host"], email["port"], email["to"]) == ("custom", "smtp.corp.ru", 587, "b@corp.ru")
    assert a.email_save({"preset": "custom", "host": " "})["ok"] is False
    a.email_save({"preset": "custom", "host": "h", "port": 99999})
    assert config.get("email")["port"] == 465


def test_analyst_save_coerces(make_api):
    a, _, config, _ = make_api()
    a.analyst_save({"enabled": False, "min_score": "75", "top_n": 0, "profile": None})
    s = config.get("analyst")
    assert (s["min_score"], s["top_n"], s["profile"]) == (75, 3, "")


# ---------- Telegram ----------

def test_telegram_connect_without_token(make_api):
    a, _, _, _ = make_api()
    res = a.telegram_connect("")
    assert res["ok"] is False and "токен" in res["text"].lower()


def test_telegram_connect_when_already_paired(make_api, secrets, monkeypatch):
    a, _, config, _ = make_api()
    secrets[notify.TELEGRAM_KEY] = "123:ABC"
    config.update({"telegram": {"chat_id": 42, "bot_name": "my_bot", "pair_code": ""}})
    monkeypatch.setattr(a._telegram, "start", lambda: None)
    res = a.telegram_connect("")
    assert res == {"ok": True, "code": "", "text": "Бот @my_bot подключён, чат уже привязан."}


def test_telegram_message_commands(make_api):
    a, _, _, _ = make_api()
    assert "Поставка проектора" in a._on_telegram_message("/zakupki")
    assert "Отчётов аналитики пока нет" in a._on_telegram_message("/analytics")
    assert a._on_telegram_message("который час").startswith("Сейчас ")


def test_reminder_is_spoken_and_sent_to_telegram(make_api, monkeypatch):
    a, window, config, _ = make_api()
    said, sent = [], []
    monkeypatch.setattr(a._speaker, "say", said.append)
    config.update({"telegram": {"chat_id": 7}})
    monkeypatch.setattr(a._telegram, "send", lambda text, chat_id=None: sent.append(text) or True)
    a._toolbox._on_reminder("позвонить")
    assert window.named("reminder") == [{"text": "позвонить"}]
    assert said == ["Сэр, напоминаю: позвонить"] and sent == ["⏰ Напоминание: позвонить"]


# ---------- ярлык и прочее ----------

def test_ensure_shortcut_runs_once(make_api, monkeypatch):
    a, _, _, _ = make_api()
    calls = []
    import jarvis_app.shortcut as sc

    monkeypatch.setattr(sc, "create_desktop_shortcut", lambda: calls.append(1) or "C:/x.lnk")
    a.ensure_shortcut()
    a.ensure_shortcut("window")  # pywebview может передать окно в обработчик loaded
    assert wait_for(lambda: a._config.get("shortcut_created"))
    assert calls == [1]


def test_remote_toolbox(make_api):
    a, _, _, _ = make_api()
    remote = api_mod.RemoteToolBox(a._toolbox)
    with pytest.raises(tools_mod.ToolError):
        remote.execute("prepare_tender", {})  # нет ни номера, ни выбранной закупки
    copy = api_mod.RemoteToolBox.__new__(api_mod.RemoteToolBox)
    with pytest.raises(AttributeError):
        copy.anything  # без бесконечной рекурсии в __getattr__


def test_default_minus_words_exposed(make_api):
    a, _, _, _ = make_api()
    assert a.get_state()["default_minus_words"] == DEFAULT_MINUS_WORDS
