"""Рабочее место заявки: закупка → документы ТЗ → разбор → подбор оборудования → черновики документов.

Состояние каждой заявки хранится в Документы\\Jarvis\\Заявки\\<номер>\\state.json.
"""

from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import doc_builder, tender_ai, tender_docs
from .config import Config, app_data_dir, documents_dir
from .llm import LLM
from .procurement import ProcurementClient, SearchParams

EventSink = Callable[[str, dict[str, Any]], None]


class TenderError(Exception):
    pass


def catalog_path() -> Path:
    return app_data_dir() / "catalog.json"


def load_catalog() -> list[dict[str, Any]]:
    try:
        data = json.loads(catalog_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


class TenderWorkspace:
    def __init__(self, config: Config, emit: EventSink) -> None:
        self._config = config
        self._emit = emit
        self._lock = threading.Lock()
        self._pipeline_lock = threading.Lock()
        self._gen = 0  # номер выбранной закупки: конвейер старой закупки останавливается
        self.state: dict[str, Any] = self._empty()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "purchase": None, "files": [], "text_chars": 0, "analysis": None, "matches": [], "documents": [],
            "estimate": None, "totals": None, "steps": {},
        }

    # ---------- хранение ----------
    def folder(self) -> Path:
        purchase = self.state.get("purchase") or {}
        number = tender_docs.safe_name(str(purchase.get("number") or "без_номера"))
        path = documents_dir() / "Заявки" / number
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _save(self) -> None:
        (self.folder() / "state.json").write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")

    def _text_path(self) -> Path:
        return self.folder() / "tz_text.txt"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = json.loads(json.dumps(self.state, ensure_ascii=False))
        data["catalog_count"] = len(load_catalog())
        data["folder"] = str(self.folder()) if self.state.get("purchase") else ""
        return data

    def _publish(self, status: str = "", busy: bool = False) -> None:
        self._emit("tender_state", {"state": self.snapshot(), "status": status, "busy": busy})

    def _progress(self, text: str) -> None:
        self._emit("tender_status", {"text": text, "busy": True})

    # ---------- закупка ----------
    def set_purchase(self, purchase: dict[str, Any]) -> None:
        if not purchase or not purchase.get("number"):
            raise TenderError("Не выбрана закупка.")
        with self._lock:
            self._gen += 1
            self.state = self._empty()
            self.state["purchase"] = {
                k: purchase.get(k) for k in ("number", "url", "law", "method", "title", "customer", "price", "deadline", "status")
            }
            saved = self.folder() / "state.json"
            if saved.exists():  # продолжаем ранее начатую работу по этой закупке
                try:
                    stored = json.loads(saved.read_text(encoding="utf-8"))
                    if isinstance(stored, dict):
                        stored["purchase"] = self.state["purchase"]
                        self.state.update(stored)
                except (OSError, json.JSONDecodeError):
                    pass
            self._save()
        self._publish("Закупка выбрана.")

    def find_purchase(self, value: str, client: ProcurementClient) -> dict[str, Any]:
        """Находит закупку по номеру или ссылке через поиск ЕИС."""
        value = (value or "").strip()
        number = "".join(ch for ch in value if ch.isdigit()) if not value.startswith("http") else ""
        if value.startswith("http"):
            url = tender_docs.purchase_url_from_input(value)
            if not url:
                raise TenderError("Это не ссылка на zakupki.gov.ru.")
            import re

            match = re.search(r"regNumber=(\d+)", url)
            number = match.group(1) if match else ""
            if not number:
                return {"number": "по_ссылке", "url": url, "title": "Закупка по ссылке"}
        if len(number) < 11:
            raise TenderError("Введите номер закупки (11 или 19 цифр) или ссылку на неё.")
        found = client.search(SearchParams(query=number, only_active=False))
        for item in found:
            if item.number == number:
                return item.to_dict()
        url = tender_docs.purchase_url_from_input(number)
        return {"number": number, "url": url, "title": "Закупка (не найдена в поиске ЕИС)"}

    # ---------- документы ----------
    def _ingest(self, paths: list[Path]) -> None:
        work = self.folder() / "files"
        work.mkdir(parents=True, exist_ok=True)
        for path in paths:
            target = work / tender_docs.safe_name(path.name)
            if path.resolve() != target.resolve():
                shutil.copy2(path, target)
        self._progress("Извлекаю текст из документов…")
        # Каждый раз разбираем все файлы заявки, чтобы ТЗ всегда шло первым, а контракт — выжимкой.
        all_files = sorted(p for p in work.iterdir() if p.is_file())
        files, text = tender_docs.process_files(all_files, work)
        with self._lock:
            self.state["files"] = [f.to_dict() for f in files]
            self._text_path().write_text(text, encoding="utf-8")
            self.state["text_chars"] = len(text)
            self.state.update(analysis=None, matches=[], estimate=None, totals=None, documents=[])
            self._save()

    def add_files(self, paths: list[str]) -> None:
        if not self.state.get("purchase"):
            raise TenderError("Сначала выберите закупку.")
        self._ingest([Path(p) for p in paths if p and Path(p).is_file()])
        self._publish("Файлы добавлены.")

    def fetch_from_eis(self, client: ProcurementClient) -> None:
        purchase = self.state.get("purchase") or {}
        if not purchase.get("url"):
            raise TenderError("У закупки нет ссылки на ЕИС.")
        self._progress("Скачиваю документы с ЕИС…")
        downloaded = tender_docs.fetch_eis_documents(client, purchase["url"], self.folder() / "files")
        self._ingest(downloaded)
        self._publish(f"Скачано файлов: {len(downloaded)}.")

    def clear_files(self) -> None:
        with self._lock:
            shutil.rmtree(self.folder() / "files", ignore_errors=True)
            self._text_path().unlink(missing_ok=True)
            self.state.update(files=[], text_chars=0, analysis=None, matches=[], documents=[], estimate=None, totals=None, steps={})
            self._save()
        self._publish("Файлы очищены.")

    # ---------- ИИ ----------
    def analyze(self) -> None:
        if not self._text_path().exists() or not self.state.get("text_chars"):
            raise TenderError("Нет текста ТЗ. Скачайте документы с ЕИС или выберите файлы.")
        text = self._text_path().read_text(encoding="utf-8")
        result = tender_ai.analyze_tz(LLM(self._config), text, on_progress=self._progress)
        with self._lock:
            self.state["analysis"] = result
            self.state["matches"] = []
            self.state["estimate"] = None
            self.state["totals"] = None
            self._save()
        count = len(result.get("positions") or [])
        note = " Документ длинный — разобраны первые части." if result.get("truncated") else ""
        self._publish(f"ТЗ разобрано: позиций {count}.{note}")

    def match(self) -> None:
        analysis = self.state.get("analysis")
        if not analysis:
            raise TenderError("Сначала разберите ТЗ.")
        catalog = load_catalog()
        matches = tender_ai.match_equipment(LLM(self._config), analysis, catalog, on_progress=self._progress)
        with self._lock:
            self.state["matches"] = matches
            self._save()
        source = f"из каталога ({len(catalog)} поз.)" if catalog else "по знаниям ИИ — обязательно проверьте"
        self._publish(f"Оборудование подобрано {source}.")

    # ---------- документы ----------
    def build_documents(self) -> None:
        purchase = self.state.get("purchase") or {}
        analysis = self.state.get("analysis") or {}
        matches = self.state.get("matches") or []
        if not analysis:
            raise TenderError("Сначала разберите ТЗ.")
        company = self._config.get("company", {}) or {}
        out = self.folder()
        self._progress("Формирую документы…")
        made = [doc_builder.build_checklist(out / "Сводка ТЗ и чек-лист.docx", purchase, analysis, matches)]
        if matches:
            made.append(doc_builder.build_offer(out / "Предложение участника.docx", purchase, analysis, matches, company))
            made.append(doc_builder.build_commercial(out / "Коммерческое предложение.docx", purchase, matches, company))
        with self._lock:
            self.state["documents"] = [str(p) for p in made]
            self._save()
        hint = "" if matches else " Подберите оборудование, чтобы получить предложение участника и КП."
        self._publish(f"Готово документов: {len(made)}.{hint}")

    # ---------- конвейер: всё по очереди ----------
    STEPS = ("files", "analysis", "match", "estimate", "docs")
    STEP_TITLES = {
        "files": "Документы", "analysis": "Разбор ТЗ", "match": "Подбор оборудования",
        "estimate": "Стоимость и маржа", "docs": "Документы заявки",
    }

    def _step(self, name: str, status: str, note: str = "") -> None:
        with self._lock:
            self.state.setdefault("steps", {})[name] = {"status": status, "note": note}
            self._save()
        self._publish(f"{self.STEP_TITLES[name]}: {note}" if note else "", busy=status == "running")

    def run_pipeline(self, client: ProcurementClient, on_report: Callable[[dict[str, Any]], None] | None = None) -> None:
        """Скачивание → разбор ТЗ → подбор → стоимость → документы. Готовые шаги пропускаются."""
        my_gen = self._gen
        with self._pipeline_lock:
            if my_gen != self._gen or not self.state.get("purchase"):
                return

            def alive() -> bool:
                return my_gen == self._gen

            # 1. документы
            if not self.state.get("text_chars"):
                self._step("files", "running", "скачиваю с ЕИС…")
                try:
                    self.fetch_from_eis(client)
                except Exception as exc:
                    if alive():
                        self._step("files", "error", f"{exc} Добавьте файлы кнопкой «Выбрать файлы» — разбор продолжится сам.")
                    return
                if not self.state.get("text_chars"):
                    if alive():
                        self._step("files", "error", "в файлах нет текста (возможно, сканы). Добавьте файлы с текстом.")
                    return
            if not alive():
                return
            self._step("files", "done", f"{len(self.state.get('files') or [])} файл(ов)")
            # 2. разбор ТЗ
            if not self.state.get("analysis"):
                self._step("analysis", "running", "читаю ТЗ…")
                try:
                    self.analyze()
                except Exception as exc:
                    if alive():
                        self._step("analysis", "error", str(exc))
                    return
            if not alive():
                return
            positions = (self.state.get("analysis") or {}).get("positions") or []
            self._step("analysis", "done", f"позиций: {len(positions)}")
            # 3. подбор
            if positions and not self.state.get("matches"):
                self._step("match", "running", "подбираю модели…")
                try:
                    self.match()
                except Exception as exc:
                    if alive():
                        self._step("match", "error", str(exc))
            if not alive():
                return
            if self.state.get("matches"):
                self._step("match", "done", f"подобрано: {len(self.state['matches'])}")
            elif positions:
                pass
            else:
                self._step("match", "skipped", "в ТЗ нет позиций оборудования")
            # 4. стоимость
            if not self.state.get("estimate"):
                self._step("estimate", "running", "считаю себестоимость и маржу…")
                try:
                    from .analyst import estimate_costs

                    estimate, totals = estimate_costs(LLM(self._config), self.state["purchase"], self.state["analysis"])
                    with self._lock:
                        self.state.update(estimate=estimate, totals=totals)
                        self._save()
                except Exception as exc:
                    if alive():
                        self._step("estimate", "error", str(exc))
            if not alive():
                return
            if self.state.get("estimate"):
                self._step("estimate", "done", "готово")
            # 5. документы Word
            self._step("docs", "running", "формирую черновики…")
            try:
                self.build_documents()
                self._step("docs", "done", f"файлов: {len(self.state.get('documents') or [])}")
            except Exception as exc:
                if alive():
                    self._step("docs", "error", str(exc))
            if on_report and alive() and self.state.get("analysis"):
                on_report({
                    "purchase": dict(self.state["purchase"]), "analysis": self.state.get("analysis"),
                    "estimate": self.state.get("estimate"), "totals": self.state.get("totals"),
                    "at": datetime.now().isoformat(timespec="seconds"), "error": "",
                    "source": "заявка",
                })
            self._publish("Готово: разбор, подбор, расчёт и документы — ниже.")

    def import_catalog(self, path: str) -> int:
        items = tender_ai.load_catalog_xlsx(Path(path))
        catalog_path().write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        self._publish(f"Каталог загружен: {len(items)} позиций.")
        return len(items)
