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
