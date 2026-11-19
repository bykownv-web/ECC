"""ToolBox, утилиты, управление ПК, голос, документы и ярлык: регрессии найденных ошибок."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from jarvis_app import doc_builder, pc_control, shortcut, tools, utilities, voice
from jarvis_app.config import Config
from jarvis_app.procurement import ProcurementError, Purchase
from jarvis_app.relevance import DEFAULT_MINUS_WORDS


@pytest.fixture
def box(tmp_path):
    events = []
    config = Config(tmp_path / "c.json")
    tb = tools.ToolBox(config, lambda e, p: events.append((e, p)))
    yield tb, config, events
    tb.reminders.cancel_all()


# ---------- ToolBox ----------

@pytest.mark.parametrize("value,expected", [
    (5, 5.0), ("5", 5.0), ("5,5", 5.5), ("1 000 000", 1_000_000.0), ("пять", 5.0), ("10 минут", 10.0),
    (None, None), ("", None), ("много", None), (True, None), (float("nan"), None), (float("inf"), None),
])
def test_to_number(value, expected):
    assert tools.to_number(value) == expected


def test_execute_coerces_arguments(box, monkeypatch):
    tb, _, _ = box
    calls = []
    monkeypatch.setattr(pc_control, "volume", lambda action, steps: calls.append((action, steps)) or "ок")
    monkeypatch.setattr(pc_control, "power", lambda action, delay: calls.append((action, delay)) or "ок")
    tb.execute("set_volume", {"action": "up", "steps": "пять"})
    tb.execute("set_volume", {"action": "up", "steps": None})
    tb.execute("set_volume", {"action": "up", "steps": "3"})
    tb.execute("power", {"action": "cancel", "delay_sec": "abc"})
    assert calls == [("up", 5), ("up", 5), ("up", 3), ("cancel", 60)]


def test_execute_reminder_minutes(box):
    tb, _, _ = box
    assert tb.execute("set_reminder", {"text": "чай", "minutes": "пять"}).startswith("Напомню в ")
    assert tb.execute("set_reminder", {"text": None, "minutes": "2"}).endswith("время вышло.")
    with pytest.raises(tools.ToolError, match="Через сколько"):
        tb.execute("set_reminder", {"text": "чай", "minutes": None})
    with pytest.raises(tools.ToolError):
        tb.execute("set_reminder", {"text": "чай", "minutes": -1})
    assert len(tb.reminders.list()) == 2


def test_execute_bad_args_shape(box, monkeypatch):
    tb, _, _ = box
    assert tb.execute("get_time", None).startswith("Сейчас ")
    assert tb.execute("get_time", ["мусор"]).startswith("Сейчас ")
    monkeypatch.setattr(pc_control, "open_url", lambda url: (_ for _ in ()).throw(TypeError("bad")))
    with pytest.raises(tools.ToolError, match="неверные параметры"):
        tb.execute("open_url", {"url": 5})
    with pytest.raises(tools.ToolError, match="Неизвестное действие"):
        tb.execute("format_disk", {})


def test_prepare_tender_hook(box):
    tb, _, _ = box
    with pytest.raises(tools.ToolError):
        tb.execute("prepare_tender", {"number": "1"})
    got = []
    tb.tender_runner = lambda number: got.append(number) or "Запустил"
    assert tb.execute("prepare_tender", {"number": 32312345678}) == "Запустил"
    assert tb.execute("prepare_tender", {}) == "Запустил"
    assert got == ["32312345678", ""]


class FakeClient:
    def __init__(self, *a, **kw):
        self.kwargs = None

    def search_tags(self, tags, **kwargs):
        FakeClient.last = (tags, kwargs)
        return [Purchase(number="1", url="https://zakupki.gov.ru/1", title="Проектор")], ["Скрыто 2 нерелевантных", "ошибка 223"]


def test_run_procurement_search_settings_and_payload(box, monkeypatch):
    tb, config, events = box
    monkeypatch.setattr(tools, "ProcurementClient", FakeClient)
    found, errors = tb.run_procurement_search()
    tags, kw = FakeClient.last
    assert kw["strict"] is True  # по умолчанию строгий режим
    assert kw["minus_words"] == DEFAULT_MINUS_WORDS  # None в настройках → стандартный список, а не пустой
    assert kw["pages_per_tag"] == 2 and kw["price_from"] is None
    results = [p for e, p in events if e == "procurement_results"][-1]
    assert set(results) == {"items", "analytics", "errors", "notes"}
    assert results["items"][0]["number"] == "1" and results["errors"] == ["ошибка 223"]
    assert results["notes"] == ["Скрыто 2 нерелевантных"]
    assert [p for e, p in events if e == "procurement_status"][-1] == {"text": "", "busy": False}

    config.update({"procurement": {"minus_words": [], "strict": False, "price_from": "100 000", "pages_per_tag": "abc"}})
    tb.run_procurement_search({"tags": "экран, проектор", "price_to": None})
    tags, kw = FakeClient.last
    assert tags == ["экран", " проектор".strip()] and kw["minus_words"] == [] and kw["strict"] is False
    assert kw["price_from"] == 100000 and kw["pages_per_tag"] == 1


def test_run_procurement_search_validation(box, monkeypatch):
    tb, config, _ = box
    monkeypatch.setattr(tools, "ProcurementClient", FakeClient)
    config.update({"procurement": {"fz44": False, "fz223": False}})
    with pytest.raises(ProcurementError, match="44-ФЗ"):
        tb.run_procurement_search()
    config.update({"procurement": {"tags": [], "fz44": True}})
    with pytest.raises(ProcurementError):
        tb.run_procurement_search()


def test_search_tool_with_empty_tags_uses_saved(box, monkeypatch):
    tb, _, _ = box
    monkeypatch.setattr(tools, "ProcurementClient", FakeClient)
    tb.execute("search_procurements", {"tags": []})
    assert FakeClient.last[0][0] == "проектор"


# ---------- напоминания и погода ----------

def test_reminder_fires(box):
    tb, _, events = box
    tb.reminders.add("тест", 0.5 / 60)  # полсекунды
    deadline = time.time() + 3
    while time.time() < deadline and not any(e == "reminder" for e, _ in events):
        time.sleep(0.02)
    assert ("reminder", {"text": "тест"}) in events
    assert tb.reminders.list() == []


def test_reminder_rejects_nan():
    rm = utilities.ReminderManager(lambda t: None)
    with pytest.raises(ValueError):
        rm.add("x", float("nan"))
    with pytest.raises(ValueError):
        rm.add("x", "abc")


class FakeHTTP:
    def __init__(self, geo, forecast):
        self.geo = geo
        self.forecast = forecast
        self.names = []

    def get(self, url, params=None, timeout=None):
        outer = self

        class R:
            def json(self):
                if "geocoding" in url:
                    outer.names.append(params["name"])
                    return outer.geo(params["name"])
                return outer.forecast

        return R()


def test_weather_parsing_and_city_variants(monkeypatch):
    http = FakeHTTP(
        lambda name: {"results": [{"name": "Москва", "latitude": 55.7, "longitude": 37.6}]} if name == "москва" else {},
        {"current": {"temperature_2m": -3.4, "apparent_temperature": -8.1, "weather_code": 71, "wind_speed_10m": 4.2},
         "daily": {"temperature_2m_max": [-1.0], "temperature_2m_min": [-6.0]}},
    )
    monkeypatch.setattr(utilities.requests, "get", http.get)
    text = utilities.weather("москве")
    assert http.names == ["москве", "москва"]
    assert text == "В городе Москва сейчас -3°, небольшой снег, ощущается как -8°, ветер 4 м/с, днём от -6° до -1°."


def test_weather_bad_answers(monkeypatch):
    place = {"results": [{"name": "Казань", "latitude": 1, "longitude": 2}]}
    for forecast in ({"error": True, "reason": "bad"}, ["список"], {"current": {"temperature_2m": "тепло"}}):
        monkeypatch.setattr(utilities.requests, "get", FakeHTTP(lambda n: place, forecast).get)
        text = utilities.weather("Казань")
        assert text in ("Сервис погоды сейчас недоступен.", "Погода: Казань.")
    monkeypatch.setattr(utilities.requests, "get", FakeHTTP(lambda n: {"results": []}, {}).get)
    assert utilities.weather("Нигдеград") == "Не нашёл город «Нигдеград»."


# ---------- управление ПК вне Windows ----------

@pytest.mark.skipif(sys.platform == "win32", reason="проверка поведения вне Windows")
def test_pc_control_fails_gracefully_off_windows(monkeypatch):
    opened = []
    monkeypatch.setattr(pc_control.webbrowser, "open", opened.append)
    assert pc_control.open_app("закупки", {}) == "Открываю zakupki.gov.ru."
    for call in (lambda: pc_control.volume("up", "x"), lambda: pc_control.media("next"), pc_control.lock_screen,
                 lambda: pc_control.power("shutdown", "abc"), lambda: pc_control.open_app("блокнот", {"блокнот": "notepad.exe"})):
        with pytest.raises(pc_control.PCError, match="только в Windows"):
            call()
    with pytest.raises(pc_control.PCError, match="Не знаю программу"):
        pc_control.open_app("к", {"калькулятор": "calc.exe"})  # одна буква не совпадает с «калькулятор»
    info = pc_control.system_info()
    assert "Процессор" in info or "psutil" in info


def test_speaker_and_sapi_are_noops_off_windows(monkeypatch):
    monkeypatch.setattr(voice, "IS_WINDOWS", False)
    s = voice.Speaker()
    s.say("Привет")
    assert not s.is_speaking()
    s._speak_sapi("Привет", threading.Event())  # раньше Popen(creationflags=…) падал ValueError


def test_neural_unexpected_error_is_reported(monkeypatch):
    s = voice.Speaker()
    errors, sapi = [], []
    s.on_error = errors.append
    monkeypatch.setattr(s, "_speak_neural", lambda text, stop: (_ for _ in ()).throw(RuntimeError("MCI error 263")))
    monkeypatch.setattr(s, "_speak_sapi", lambda text, stop: sapi.append(text))
    s._speak("раз", threading.Event())
    assert sapi == ["раз"] and errors and "RuntimeError" in errors[0]


def test_listener_mic_errors_become_voice_error_and_loop_survives(monkeypatch):
    monkeypatch.setattr(voice, "LISTEN_RETRY_SEC", 0.01)
    class Mic:
        def __enter__(self):
            raise OSError("Invalid input device")

        def __exit__(self, *a):
            return False

    class SR:
        class WaitTimeoutError(Exception):
            pass

        @staticmethod
        def Microphone():
            return Mic()

    listener = voice.Listener.__new__(voice.Listener)
    listener._sr = SR
    listener._recognizer = None
    listener._background_stop = None
    listener._mic_lock = threading.Lock()
    with pytest.raises(voice.VoiceError, match="микрофон"):
        listener.listen_once()

    calls, errors = [], []

    def flaky(timeout=5, phrase_limit=12):
        calls.append(1)
        if len(calls) == 1:
            raise TypeError("неожиданная ошибка")
        return "джарвис который час"

    listener.listen_once = flaky
    commands = []

    def on_command(command):
        commands.append(command)
        listener.stop_background()

    listener.start_background("джарвис", on_command, errors.append, lambda: False)
    deadline = time.time() + 3
    while time.time() < deadline and not commands:
        time.sleep(0.02)
    assert errors and "TypeError" in errors[0]
    assert commands == ["который час"]


# ---------- документы ----------

def test_price_parsing():
    p = doc_builder._price_number
    assert p("450 000,00") == 450000.0 and p("1 234,5 ₽") == 1234.5 and p("1,234.56") == 1234.56
    assert p("1.234.567,89") == 1234567.89 and p("12 500 руб.") == 12500.0 and p(450000) == 450000.0
    assert p(None) is None and p("уточнить") is None
    assert doc_builder._qty_number("2 шт") == 2.0 and doc_builder._qty_number(None) == 1.0


def test_documents_tolerate_missing_and_none(tmp_path):
    from jarvis_app import tender_docs

    purchase = {"number": None, "law": None, "title": None, "customer": None, "price": "1 000 000,00"}
    analysis = {"positions": None, "restrictions": "Запрет на иностранные товары", "summary": None}
    matches = [
        {"position_index": None, "position": None, "brand": None, "model": 5, "price": "450 000", "quantity": None,
         "offered_values": [None, {"name": None}, {"name": "Яркость", "value": None}]},
        {"position_index": "7", "position": "Экран"},
        "мусор",
    ]
    company = {"name": None, "inn": None, "kpp": "770101001", "director": None}
    paths = [
        doc_builder.build_offer(tmp_path / "o.docx", purchase, analysis, matches, company),
        doc_builder.build_commercial(tmp_path / "k.docx", purchase, matches, None),
        doc_builder.build_checklist(tmp_path / "c.docx", purchase, None, None),
        doc_builder.build_checklist(tmp_path / "c2.docx", purchase, analysis, matches),
    ]
    texts = [tender_docs.extract_text(p) for p in paths]
    assert all("None" not in t for t in texts)
    assert "Итого: 450 000,00 ₽" in texts[1] and "снижение 55.0%" in texts[1]
    assert "КПП 770101001" in texts[0] and "ИНН" not in texts[0]
    assert "☐ Запрет на иностранные товары" in texts[3]  # строка, а не список, — один пункт, а не по буквам


def test_commercial_over_nmck(tmp_path):
    from jarvis_app import tender_docs

    path = doc_builder.build_commercial(tmp_path / "k.docx", {"price": 100.0}, [{"price": "150", "quantity": "1"}], {})
    assert "превышение НМЦК на 50.0%" in tender_docs.extract_text(path)


# ---------- ярлык ----------

def test_shortcut_target_for_frozen_exe(monkeypatch, tmp_path):
    exe = tmp_path / "Jarvis" / "Jarvis.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    t = shortcut.shortcut_target()
    assert t["target"] == str(exe.resolve()) and t["args"] == "" and t["workdir"] == str(exe.resolve().parent)
    monkeypatch.delattr(sys, "frozen")
    t = shortcut.shortcut_target()
    assert t["args"].endswith('main.py"') and Path(t["workdir"]) == shortcut.APP_DIR


@pytest.mark.skipif(sys.platform == "win32", reason="проверка поведения вне Windows")
def test_create_shortcut_off_windows():
    with pytest.raises(OSError, match="только в Windows"):
        shortcut.create_desktop_shortcut()
