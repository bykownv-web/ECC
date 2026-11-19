"""Агенты Джарвиса: кто чем занят и живой журнал их работы для вкладки «Агенты».

Каждая часть приложения (поиск ЕИС, разбор ТЗ, подбор, смета, аналитика, мониторинг, Telegram) пишет сюда
своё состояние и события. Окно получает обновления событием «agents» не чаще нескольких раз в секунду.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterator

AGENTS: list[tuple[str, str, str, str]] = [
    # id, имя, роль, значок
    ("search", "Поисковик ЕИС", "Ищет закупки по тегам, фильтрует нерелевантные и устаревшие", "🔎"),
    ("docs", "Документовед", "Скачивает документы закупки, извлекает текст, готовит Word", "📄"),
    ("tz", "Аналитик ТЗ", "Разбирает ТЗ: позиции, сроки и место поставки, монтаж, оплата, штрафы", "🧠"),
    ("match", "Подборщик", "Подбирает оборудование под требования заказчика", "🧩"),
    ("estimate", "Сметчик", "Считает себестоимость оборудования и монтажа, маржу к НМЦК", "💰"),
    ("scout", "Аналитик закупок", "Оценивает закупки по профилю компании и выбирает лучшие", "📊"),
    ("monitor", "Монитор", "Проверяет ЕИС по расписанию и сообщает о новых закупках", "👁"),
    ("comms", "Связной", "Telegram-бот и почта: команды, уведомления, отчёты", "✉️"),
]

LEVELS = ("info", "ok", "warn", "error")
MAX_LOG = 600
EMIT_INTERVAL = 0.25


@dataclass
class AgentState:
    id: str
    name: str
    role: str
    icon: str
    status: str = "idle"  # idle | working | done | error
    task: str = ""
    progress: float | None = None
    started: float = 0.0
    updated: float = 0.0
    runs: int = 0
    errors: int = 0
    last_result: str = ""


@dataclass
class LogEntry:
    ts: float
    agent: str
    level: str
    text: str
    extra: dict[str, Any] = field(default_factory=dict)


class AgentBus:
    def __init__(self, emit: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self._emit = emit
        self._lock = threading.Lock()
        self._agents = {a[0]: AgentState(*a) for a in AGENTS}
        self._log: deque[LogEntry] = deque(maxlen=MAX_LOG)
        self._seq = 0
        self._last_emit = 0.0
        self._pending = False
        self._timer: threading.Timer | None = None

    # ---------- запись ----------
    def log(self, agent: str, text: str, level: str = "info", **extra: Any) -> None:
        if level not in LEVELS:
            level = "info"
        with self._lock:
            if agent not in self._agents:
                agent = "comms" if agent == "telegram" else "search"
            self._log.append(LogEntry(time.time(), agent, level, str(text)[:500], extra))
            self._seq += 1
            st = self._agents[agent]
            st.updated = time.time()
            if level == "error":
                st.errors += 1
        self._schedule()

    def start(self, agent: str, task: str, progress: float | None = None) -> None:
        with self._lock:
            st = self._agents.get(agent)
            if st is None:
                return
            st.status, st.task, st.progress = "working", task, progress
            st.started = st.updated = time.time()
            st.runs += 1
        self.log(agent, f"Начал: {task}")

    def progress(self, agent: str, task: str | None = None, progress: float | None = None) -> None:
        with self._lock:
            st = self._agents.get(agent)
            if st is None:
                return
            if st.status != "working":
                st.status, st.started = "working", time.time()
            if task:
                st.task = task
            if progress is not None:
                st.progress = max(0.0, min(1.0, float(progress)))
            st.updated = time.time()
        if task:
            self.log(agent, task)
        else:
            self._schedule()

    def finish(self, agent: str, result: str = "", ok: bool = True) -> None:
        with self._lock:
            st = self._agents.get(agent)
            if st is None:
                return
            took = time.time() - st.started if st.started else 0.0
            st.status = "done" if ok else "error"
            st.progress = 1.0 if ok else st.progress
            st.last_result = result
            st.updated = time.time()
        suffix = f" ({took:.1f} с)" if took >= 0.05 else ""
        self.log(agent, (f"Готово: {result}" if ok else f"Ошибка: {result}") + suffix, "ok" if ok else "error")

    def fail(self, agent: str, error: str) -> None:
        self.finish(agent, error, ok=False)

    @contextmanager
    def task(self, agent: str, title: str) -> Iterator[None]:
        """with bus.task("tz", "Разбор ТЗ"): ...  — старт, готово или ошибка с временем выполнения."""
        self.start(agent, title)
        try:
            yield
        except Exception as exc:
            self.fail(agent, str(exc) or exc.__class__.__name__)
            raise
        else:
            with self._lock:
                result = self._agents[agent].last_result if agent in self._agents else ""
            self.finish(agent, result or title)

    def idle(self, agent: str) -> None:
        with self._lock:
            st = self._agents.get(agent)
            if st is not None:
                st.status, st.task, st.progress = "idle", "", None
        self._schedule()

    # ---------- чтение ----------
    def snapshot(self, since: int = 0) -> dict[str, Any]:
        with self._lock:
            agents = [asdict(a) for a in self._agents.values()]
            log = [asdict(e) for e in self._log]
            seq = self._seq
        return {"agents": agents, "log": log[-MAX_LOG:], "seq": seq, "now": time.time()}

    # ---------- отправка в окно (с ограничением частоты) ----------
    def _schedule(self) -> None:
        if self._emit is None:
            return
        now = time.time()
        with self._lock:
            if now - self._last_emit >= EMIT_INTERVAL and not self._pending:
                self._last_emit = now
                fire_now = True
            else:
                fire_now = False
                if not self._pending:
                    self._pending = True
                    delay = max(0.01, EMIT_INTERVAL - (now - self._last_emit))
                    self._timer = threading.Timer(delay, self._flush)
                    self._timer.daemon = True
                    self._timer.start()
        if fire_now:
            self._send()

    def _flush(self) -> None:
        with self._lock:
            self._pending = False
            self._last_emit = time.time()
        self._send()

    def _send(self) -> None:
        if self._emit is not None:
            try:
                self._emit("agents", self.snapshot())
            except Exception:
                pass


# Общая шина: модули без доступа к окну пишут сюда, а окно подписывается через set_emitter.
BUS = AgentBus()


def set_emitter(emit: Callable[[str, dict[str, Any]], None]) -> None:
    BUS._emit = emit  # noqa: SLF001 — подключение окна после создания


class AgentWatch:
    """Переводит события, которые Джарвис отправляет в окно, в работу агентов и записи журнала.

    Так модули поиска, заявки и аналитики не нужно менять: их события уже описывают, что происходит.
    """

    STEP_AGENT = {"files": "docs", "analysis": "tz", "match": "match", "estimate": "estimate", "docs": "docs"}
    STEP_TITLE = {"files": "Документы закупки", "analysis": "Разбор ТЗ", "match": "Подбор оборудования",
                  "estimate": "Смета и маржа", "docs": "Документы заявки"}

    def __init__(self, bus: AgentBus) -> None:
        self.bus = bus
        self._steps: dict[str, str] = {}
        self._purchase = ""

    def observe(self, event: str, p: dict[str, Any]) -> None:
        p = p if isinstance(p, dict) else {}
        handler = getattr(self, "_on_" + event, None)
        if handler is not None:
            handler(p)

    # ---------- поиск ----------
    def _on_procurement_status(self, p: dict[str, Any]) -> None:
        text = str(p.get("text") or "")
        if p.get("busy") and text:
            if self.bus.snapshot()["agents"][0]["status"] != "working":
                self.bus.start("search", text)
            else:
                self.bus.progress("search", text)

    def _on_procurement_results(self, p: dict[str, Any]) -> None:
        items = p.get("items") or []
        errors = p.get("errors") or []
        notes = p.get("notes") or []
        for note in notes if isinstance(notes, list) else []:
            self.bus.log("search", str(note), "warn")
        if errors and isinstance(errors, list) and not items:
            self.bus.fail("search", "; ".join(map(str, errors))[:300])
        else:
            for err in errors if isinstance(errors, list) else []:
                self.bus.log("search", str(err), "warn")
            self.bus.finish("search", f"найдено актуальных закупок: {len(items)}")

    # ---------- заявка ----------
    def _on_tender_state(self, p: dict[str, Any]) -> None:
        st = p.get("state") or {}
        purchase = (st.get("purchase") or {}).get("number") or ""
        if purchase and purchase != self._purchase:
            self._purchase = purchase
            self._steps = {}
            self.bus.log("docs", f"Новая закупка в работе: № {purchase}")
        for key, info in (st.get("steps") or {}).items():
            if not isinstance(info, dict) or key not in self.STEP_AGENT:
                continue
            status, note = info.get("status", ""), str(info.get("note") or "")
            if self._steps.get(key) == status + note:
                continue
            self._steps[key] = status + note
            agent = self.STEP_AGENT[key]
            title = f"{self.STEP_TITLE[key]} № {purchase}" if purchase else self.STEP_TITLE[key]
            if status == "running":
                self.bus.start(agent, title)
                if note:
                    self.bus.progress(agent, note)
            elif status == "done":
                self.bus.finish(agent, note or title)
            elif status == "skipped":
                self.bus.log(agent, f"Пропущено: {note}", "warn")
                self.bus.idle(agent)
            elif status == "error":
                self.bus.fail(agent, note or "ошибка")

    def _on_tender_status(self, p: dict[str, Any]) -> None:
        text = str(p.get("text") or "")
        if not text:
            return
        low = text.lower()
        agent = ("tz" if "тз" in low or "разбира" in low else "match" if "подбира" in low
                 else "estimate" if "стоимост" in low or "маржу" in low else "docs")
        if p.get("error"):
            self.bus.log(agent, text, "error")
        elif p.get("busy"):
            self.bus.progress(agent, text)

    # ---------- аналитика и мониторинг ----------
    def _on_analyst_status(self, p: dict[str, Any]) -> None:
        text = str(p.get("text") or "")
        if not text:
            return
        if p.get("error"):
            self.bus.fail("scout", text)
            return
        if p.get("busy"):
            import re as _re

            m = _re.search(r"(\d+)\s+из\s+(\d+)", text)
            frac = int(m.group(1)) / max(1, int(m.group(2))) if m else None
            low = text.lower()
            agent = "tz" if "тз" in low else "estimate" if "стоимост" in low else "docs" if "скачива" in low else "scout"
            self.bus.progress(agent, text, frac)
        elif text.startswith("Готово"):
            self.bus.finish("scout", text)

    def _on_monitor_new(self, p: dict[str, Any]) -> None:
        self.bus.log("monitor", str(p.get("text") or "Новые закупки"), "ok")

    # ---------- связь ----------
    def _on_telegram_status(self, p: dict[str, Any]) -> None:
        text = str(p.get("text") or "")
        if text:
            self.bus.log("comms", text, "error" if p.get("error") else "ok")

    def _on_user(self, p: dict[str, Any]) -> None:
        text = str(p.get("text") or "")
        if text.startswith("[Telegram]"):
            self.bus.log("comms", "Входящее: " + text[10:].strip()[:200])

    def _on_reminder(self, p: dict[str, Any]) -> None:
        self.bus.log("comms", "Напоминание: " + str(p.get("text") or ""), "ok")
