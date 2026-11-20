import time

import pytest

from jarvis_app.agents import AgentBus


def test_lifecycle_and_log():
    events = []
    bus = AgentBus(lambda e, p: events.append(p))
    bus.start("tz", "Разбор ТЗ № 1")
    bus.progress("tz", "часть 1 из 2", 0.5)
    bus.finish("tz", "позиций: 3")
    snap = bus.snapshot()
    tz = next(a for a in snap["agents"] if a["id"] == "tz")
    assert tz["status"] == "done" and tz["progress"] == 1.0 and tz["runs"] == 1 and tz["last_result"] == "позиций: 3"
    texts = [e["text"] for e in snap["log"]]
    assert texts[0] == "Начал: Разбор ТЗ № 1" and texts[1] == "часть 1 из 2" and texts[2].startswith("Готово: позиций: 3")
    time.sleep(0.4)
    assert events and events[-1]["seq"] == snap["seq"]  # последнее состояние доставлено


def test_task_context_records_errors():
    bus = AgentBus()
    with pytest.raises(ValueError):
        with bus.task("docs", "Скачивание"):
            raise ValueError("нет файлов")
    docs = next(a for a in bus.snapshot()["agents"] if a["id"] == "docs")
    assert docs["status"] == "error" and docs["errors"] == 1
    assert bus.snapshot()["log"][-1]["level"] == "error"


def test_emit_is_throttled():
    events = []
    bus = AgentBus(lambda e, p: events.append(p))
    for i in range(200):
        bus.log("search", f"строка {i}")
    time.sleep(0.4)
    assert 1 <= len(events) <= 5
    assert events[-1]["log"][-1]["text"] == "строка 199"


def test_unknown_agent_and_level_are_safe():
    bus = AgentBus()
    bus.log("nobody", "x", level="weird")
    bus.start("nobody", "x")
    assert bus.snapshot()["log"][-1]["level"] == "info"


def test_watch_maps_pipeline_events_to_agents():
    from jarvis_app.agents import AgentWatch

    bus = AgentBus()
    w = AgentWatch(bus)
    w.observe("procurement_status", {"text": "Ищу «проектор», страница 1…", "busy": True})
    w.observe("procurement_results", {"items": [1, 2], "errors": [], "notes": ["Скрыто не по теме: 5"]})
    w.observe("tender_state", {"state": {"purchase": {"number": "32616576327"},
                                         "steps": {"files": {"status": "done", "note": "7 файл(ов)"},
                                                   "analysis": {"status": "running", "note": "читаю ТЗ…"}}}})
    w.observe("tender_state", {"state": {"purchase": {"number": "32616576327"},
                                         "steps": {"analysis": {"status": "error", "note": "JSON"}}}})
    w.observe("analyst_status", {"text": "Оцениваю закупки: 15 из 30…", "busy": True})
    w.observe("telegram_status", {"text": "Telegram привязан", "paired": True})
    agents = {a["id"]: a for a in bus.snapshot()["agents"]}
    assert agents["search"]["status"] == "done" and "2" in agents["search"]["last_result"]
    assert agents["docs"]["status"] == "done"
    assert agents["tz"]["status"] == "error"
    assert agents["scout"]["status"] == "working" and agents["scout"]["progress"] == 0.5
    texts = " | ".join(e["text"] for e in bus.snapshot()["log"])
    assert "Скрыто не по теме" in texts and "Telegram привязан" in texts and "№ 32616576327" in texts
    w.observe("unknown_event", {"x": 1})  # неизвестные события не ломают журнал
