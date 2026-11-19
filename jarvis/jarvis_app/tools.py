"""Набор действий Джарвиса. Ими пользуются и ИИ (через tool use), и локальные команды."""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable

from . import pc_control, utilities
from .config import Config, app_data_dir, documents_dir
from .relevance import DEFAULT_MINUS_WORDS, parse_minus_words
from .procurement import (
    ProcurementClient,
    ProcurementError,
    Purchase,
    SeenStore,
    analyze,
    export_to_excel,
    summary_text,
    timestamp,
)


class ToolError(Exception):
    pass


EventSink = Callable[[str, dict[str, Any]], None]


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search_procurements",
        "description": (
            "Ищет текущие закупки в ЕИС (zakupki.gov.ru) по 44-ФЗ и 223-ФЗ по ключевым словам. "
            "Если пользователь не назвал слова, не передавай tags — возьмутся его сохранённые теги. "
            "Результаты также показываются в таблице на вкладке «Закупки»."
        ),
        "input_schema": _schema(
            {
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Ключевые слова, например «проектор»."},
                "fz44": {"type": "boolean", "description": "Искать по 44-ФЗ (по умолчанию да)."},
                "fz223": {"type": "boolean", "description": "Искать по 223-ФЗ (по умолчанию да)."},
                "only_active": {"type": "boolean", "description": "Только этап подачи заявок (по умолчанию да)."},
                "price_from": {"type": "number", "description": "Минимальная НМЦК в рублях."},
                "price_to": {"type": "number", "description": "Максимальная НМЦК в рублях."},
            }
        ),
    },
    {
        "name": "prepare_tender",
        "description": (
            "Полный разбор конкретной закупки: скачивает документы с ЕИС, разбирает ТЗ (сроки и место поставки, "
            "монтаж, оплата, обеспечение), подбирает оборудование, считает стоимость и маржу, готовит документы Word. "
            "Вызывай, когда пользователь даёт номер закупки (11 или 19 цифр) или ссылку на zakupki.gov.ru, "
            "или просит разобрать ТЗ текущей закупки (тогда number не передавай). Работа идёт в фоне на вкладке «Заявка»."
        ),
        "input_schema": _schema({"number": {"type": "string", "description": "Номер закупки или ссылка."}}),
    },
    {
        "name": "procurement_analytics",
        "description": "Аналитика по последним найденным закупкам: суммы, законы, теги, крупнейшие заказчики, близкие сроки подачи.",
        "input_schema": _schema({}),
    },
    {
        "name": "export_procurements",
        "description": "Сохраняет последние найденные закупки в Excel в папку Документы\\Jarvis и открывает файл.",
        "input_schema": _schema({}),
    },
    {
        "name": "open_app",
        "description": "Открывает программу на компьютере по названию (блокнот, калькулятор, проводник, word, excel и программы из настроек).",
        "input_schema": _schema({"name": {"type": "string"}}, ["name"]),
    },
    {
        "name": "open_url",
        "description": "Открывает сайт в браузере. Только адреса http:// или https://.",
        "input_schema": _schema({"url": {"type": "string"}}, ["url"]),
    },
    {
        "name": "web_search",
        "description": "Открывает поиск Яндекса в браузере по запросу.",
        "input_schema": _schema({"query": {"type": "string"}}, ["query"]),
    },
    {
        "name": "set_volume",
        "description": "Меняет громкость: up — громче, down — тише, mute — выключить или включить звук.",
        "input_schema": _schema(
            {
                "action": {"type": "string", "enum": ["up", "down", "mute"]},
                "steps": {"type": "integer", "description": "Сколько шагов по 2% (по умолчанию 5)."},
            },
            ["action"],
        ),
    },
    {
        "name": "media_control",
        "description": "Управляет воспроизведением: пауза/продолжить, следующий или предыдущий трек.",
        "input_schema": _schema({"action": {"type": "string", "enum": ["play_pause", "next", "prev"]}}, ["action"]),
    },
    {
        "name": "lock_screen",
        "description": "Блокирует компьютер.",
        "input_schema": _schema({}),
    },
    {
        "name": "power",
        "description": (
            "Выключает или перезагружает компьютер с задержкой, либо отменяет это (cancel). "
            "Вызывай только по прямой просьбе пользователя."
        ),
        "input_schema": _schema(
            {
                "action": {"type": "string", "enum": ["shutdown", "restart", "cancel"]},
                "delay_sec": {"type": "integer", "description": "Задержка в секундах, от 30 (по умолчанию 60)."},
            },
            ["action"],
        ),
    },
    {
        "name": "get_time",
        "description": "Текущие время и дата.",
        "input_schema": _schema({}),
    },
    {
        "name": "get_weather",
        "description": "Текущая погода в городе. Если город не назван, не передавай city — возьмётся город из настроек.",
        "input_schema": _schema({"city": {"type": "string"}}),
    },
    {
        "name": "set_reminder",
        "description": "Ставит напоминание или таймер через указанное число минут.",
        "input_schema": _schema(
            {"text": {"type": "string"}, "minutes": {"type": "number"}},
            ["text", "minutes"],
        ),
    },
    {
        "name": "list_reminders",
        "description": "Показывает активные напоминания.",
        "input_schema": _schema({}),
    },
    {
        "name": "system_info",
        "description": "Загрузка процессора, памяти, свободное место на диске, заряд батареи.",
        "input_schema": _schema({}),
    },
]


class ToolBox:
    def __init__(self, config: Config, emit: EventSink) -> None:
        self._config = config
        self._emit = emit
        self._results: list[Purchase] = []
        self._results_lock = threading.Lock()
        self.reminders = utilities.ReminderManager(self._on_reminder)
        self.tender_runner: Callable[..., str] | None = None  # задаёт окно приложения
        self.seen = SeenStore(app_data_dir() / "seen_purchases.json")

    # ---------- закупки ----------
    def procurement_client(self) -> ProcurementClient:
        return self._client()

    def _client(self) -> ProcurementClient:
        return ProcurementClient(ca_bundle=self._config.get("procurement", {}).get("ca_bundle", ""))

    def run_procurement_search(self, overrides: dict[str, Any] | None = None) -> tuple[list[Purchase], list[str]]:
        settings = self._config.get("procurement", {})
        settings.update({k: v for k, v in (overrides or {}).items() if v is not None})
        tags = settings.get("tags") or []
        if isinstance(tags, str):
            tags = [t for t in tags.split(",")]
        tags = [str(t).strip() for t in tags if str(t).strip()]
        if not tags:
            raise ProcurementError("Не заданы ключевые слова для поиска.")
        self._emit("procurement_status", {"text": "Запрос к ЕИС…", "busy": True})
        try:
            found, errors = self._client().search_tags(
                tags,
                fz44=bool(settings.get("fz44", True)),
                fz223=bool(settings.get("fz223", True)),
                only_active=bool(settings.get("only_active", True)),
                price_from=settings.get("price_from") or None,
                price_to=settings.get("price_to") or None,
                pages_per_tag=int(settings.get("pages_per_tag", 1) or 1),
                strict=bool(settings.get("strict", True)),
                minus_words=parse_minus_words(settings.get("minus_words", DEFAULT_MINUS_WORDS)),
                on_progress=lambda text: self._emit("procurement_status", {"text": text, "busy": True}),
            )
        finally:
            self._emit("procurement_status", {"text": "", "busy": False})
        with self._results_lock:
            self._results = found
        notes = [e for e in errors if e.startswith("Скрыто")]
        errors = [e for e in errors if not e.startswith("Скрыто")]
        self._emit(
            "procurement_results",
            {"items": [p.to_dict() for p in found], "analytics": analyze(found), "errors": errors, "notes": notes},
        )
        return found, errors + notes

    def last_results(self) -> list[Purchase]:
        with self._results_lock:
            return list(self._results)

    def export_results(self) -> str:
        results = self.last_results()
        if not results:
            raise ProcurementError("Сначала найдите закупки.")
        path = export_to_excel(results, documents_dir() / f"zakupki_{timestamp()}.xlsx")
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass
        return str(path)

    # ---------- напоминания ----------
    def _on_reminder(self, text: str) -> None:
        self._emit("reminder", {"text": text})

    # ---------- общий вход для ИИ ----------
    def execute(self, name: str, args: dict[str, Any]) -> str:
        args = args or {}
        try:
            return self._dispatch(name, args)
        except (pc_control.PCError, ProcurementError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    def _dispatch(self, name: str, args: dict[str, Any]) -> str:
        if name == "search_procurements":
            overrides = {k: args.get(k) for k in ("tags", "fz44", "fz223", "only_active", "price_from", "price_to")}
            found, errors = self.run_procurement_search(overrides)
            text = summary_text(found)
            if errors:
                text += "\nОшибки: " + "; ".join(errors)
            return text
        if name == "prepare_tender":
            if self.tender_runner is None:
                raise ToolError("Разбор закупок доступен только в окне Джарвиса.")
            return self.tender_runner(str(args.get("number") or ""))
        if name == "procurement_analytics":
            results = self.last_results()
            if not results:
                return "Закупки ещё не искали. Сначала вызови search_procurements."
            return json.dumps(analyze(results), ensure_ascii=False)
        if name == "export_procurements":
            return f"Файл сохранён: {self.export_results()}"
        if name == "open_app":
            return pc_control.open_app(str(args.get("name", "")), self._config.get("apps", {}))
        if name == "open_url":
            return pc_control.open_url(str(args.get("url", "")))
        if name == "web_search":
            return pc_control.web_search(str(args.get("query", "")))
        if name == "set_volume":
            return pc_control.volume(str(args.get("action", "")), int(args.get("steps") or 5))
        if name == "media_control":
            return pc_control.media(str(args.get("action", "")))
        if name == "lock_screen":
            return pc_control.lock_screen()
        if name == "power":
            return pc_control.power(str(args.get("action", "")), int(args.get("delay_sec") or 60))
        if name == "get_time":
            return utilities.now_text()
        if name == "get_weather":
            return utilities.weather(str(args.get("city") or self._config.get("city", "Москва")))
        if name == "set_reminder":
            return self.reminders.add(str(args.get("text", "")), float(args.get("minutes", 0)))
        if name == "list_reminders":
            items = self.reminders.list()
            if not items:
                return "Активных напоминаний нет."
            return "; ".join(f"{i['due']} — {i['text']}" for i in items)
        if name == "system_info":
            return pc_control.system_info()
        raise ToolError(f"Неизвестное действие: {name}")
