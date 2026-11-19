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
from .procurement import ProcurementClient, ProcurementError, SearchParams

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


class _Cancelled(Exception):
    """Пользователь выбрал другую закупку — результаты старой не записываем."""


def _plural_files(n: int) -> str:
    return "файл" if n % 10 == 1 and n % 100 != 11 else ("файла" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "файлов")


class TenderWorkspace:
    def __init__(self, config: Config, emit: EventSink) -> None:
        self._config = config
        self._emit = emit
        self._lock = threading.RLock()
        self._pipeline_lock = threading.Lock()
        self._gen = 0  # номер выбранной закупки: конвейер старой закупки останавливается
        self._running = False  # идёт конвейер: промежуточные публикации не снимают «занято» в интерфейсе
        self.state: dict[str, Any] = self._empty()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "purchase": None, "files": [], "text_chars": 0, "analysis": None, "matches": [], "documents": [],
            "estimate": None, "totals": None, "steps": {}, "download_errors": [],
        }

    # ---------- хранение ----------
    def _folder_for(self, state: dict[str, Any]) -> Path:
        purchase = state.get("purchase") or {}
        number = tender_docs.safe_name(str(purchase.get("number") or "без_номера"))
        path = documents_dir() / "Заявки" / number
        path.mkdir(parents=True, exist_ok=True)
        return path

    def folder(self) -> Path:
        return self._folder_for(self.state)

    def _save(self) -> None:
        from .analyst import write_json_atomic

        write_json_atomic(self.folder() / "state.json", self.state, indent=1)

    def _text_path(self, folder: Path | None = None) -> Path:
        return (folder or self.folder()) / "tz_text.txt"

    def _ctx(self, gen: int | None = None) -> tuple[int, dict[str, Any], Path]:
        """Снимок «какая закупка сейчас»: номер поколения, её состояние и папка."""
        with self._lock:
            if gen is not None and gen != self._gen:
                raise _Cancelled()
            return self._gen, self.state, self.folder()

    def _commit(self, gen: int, **updates: Any) -> None:
        """Записывает результат шага, только если закупка за это время не сменилась."""
        with self._lock:
            if gen != self._gen:
                raise _Cancelled()
            self.state.update(updates)
            self._save()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = json.loads(json.dumps(self.state, ensure_ascii=False, default=str))
            data["folder"] = str(self.folder()) if self.state.get("purchase") else ""
        data["catalog_count"] = len(load_catalog())
        return data

    def _publish(self, status: str = "", busy: bool = False) -> None:
        self._emit("tender_state", {"state": self.snapshot(), "status": status, "busy": busy or self._running})

    def _progress(self, text: str) -> None:
        self._emit("tender_status", {"text": text, "busy": True})

    def _progress_for(self, gen: int) -> Callable[[str], None]:
        """Прогресс, который заодно прерывает долгий разбор, если пользователь выбрал другую закупку."""
        def report(text: str) -> None:
            if gen != self._gen:
                raise _Cancelled()
            self._progress(text)

        return report

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
                        stored["purchase"] = {**(stored.get("purchase") or {}), **{k: v for k, v in self.state["purchase"].items() if v}}
                        steps = stored.get("steps") if isinstance(stored.get("steps"), dict) else {}
                        # шаг «выполняется» из прошлого запуска (приложение закрыли посреди работы) — начнём его заново
                        stored["steps"] = {k: v for k, v in steps.items() if isinstance(v, dict) and v.get("status") != "running"}
                        self.state.update({k: v for k, v in stored.items() if k in self.state})
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    pass
            self._save()
        self._publish("Закупка выбрана.")

    def find_purchase(self, value: str, client: ProcurementClient) -> dict[str, Any]:
        """Находит закупку по номеру или ссылке через поиск ЕИС."""
        import re

        value = (value or "").strip()
        number = "".join(ch for ch in value if ch.isdigit()) if not value.startswith("http") else ""
        if value.startswith("http"):
            url = tender_docs.purchase_url_from_input(value)
            if not url:
                raise TenderError("Это не ссылка на zakupki.gov.ru.")
            match = re.search(r"(?:regNumber|purchaseNumber|purchaseNoticeNumber)=(\d+)", url)
            number = match.group(1) if match else ""
            if not number:
                return {"number": "по_ссылке", "url": url, "title": "Закупка по ссылке"}
        if len(number) < 11:
            raise TenderError("Введите номер закупки (11 или 19 цифр) или ссылку на неё.")
        try:
            found = client.search(SearchParams(query=number, only_active=False))
        except ProcurementError as exc:
            # Поиск ЕИС недоступен — работаем по прямой ссылке: документы можно скачать или добавить вручную.
            url = tender_docs.purchase_url_from_input(value if value.startswith("http") else number)
            return {"number": number, "url": url, "title": f"Закупка № {number} (поиск ЕИС недоступен: {exc})"}
        for item in found:
            if item.number == number:
                return item.to_dict()
        url = tender_docs.purchase_url_from_input(value if value.startswith("http") else number)
        return {"number": number, "url": url, "title": "Закупка (не найдена в поиске ЕИС)"}

    # ---------- документы ----------
    def _ingest(self, paths: list[Path], gen: int | None = None) -> None:
        gen, _, folder = self._ctx(gen)
        work = folder / "files"
        work.mkdir(parents=True, exist_ok=True)
        for path in paths:
            target = work / tender_docs.safe_name(path.name)
            if path.resolve() != target.resolve():
                shutil.copy2(path, target)
        self._progress("Извлекаю текст из документов…")
        # Каждый раз разбираем все файлы заявки, чтобы ТЗ всегда шло первым, а контракт — выжимкой.
        all_files = sorted(p for p in work.iterdir() if p.is_file() and not p.name.endswith((".tmp", ".part")))
        files, text = tender_docs.process_files(all_files, work)
        with self._lock:
            if gen != self._gen:
                raise _Cancelled()
            self._text_path(folder).write_text(text, encoding="utf-8")
        self._commit(gen, files=[f.to_dict() for f in files], text_chars=len(text),
                     analysis=None, matches=[], estimate=None, totals=None, documents=[])

    def add_files(self, paths: list[str]) -> None:
        if not self.state.get("purchase"):
            raise TenderError("Сначала выберите закупку.")
        try:
            self._ingest([Path(p) for p in paths if p and Path(p).is_file()])
        except _Cancelled:
            return
        self._publish("Файлы добавлены.")

    def fetch_from_eis(self, client: ProcurementClient, gen: int | None = None) -> None:
        try:
            gen, state, folder = self._ctx(gen)
        except _Cancelled:
            return
        purchase = state.get("purchase") or {}
        if not purchase.get("url"):
            raise TenderError("У закупки нет ссылки на ЕИС. Добавьте файлы кнопкой «Выбрать файлы».")
        self._progress("Скачиваю документы с ЕИС…")
        downloaded = tender_docs.fetch_eis_documents(client, purchase["url"], folder / "files")
        errors = list(getattr(downloaded, "errors", []))
        self._commit(gen, download_errors=errors)
        self._ingest(list(downloaded), gen)
        tail = f" Не скачались: {len(errors)} — {errors[0]}" if errors else ""
        self._publish(f"Скачано файлов: {len(downloaded)}.{tail}")

    def clear_files(self) -> None:
        with self._lock:
            shutil.rmtree(self.folder() / "files", ignore_errors=True)
            self._text_path().unlink(missing_ok=True)
            self.state.update(files=[], text_chars=0, analysis=None, matches=[], documents=[], estimate=None, totals=None,
                              steps={}, download_errors=[])
            self._save()
        self._publish("Файлы очищены.")

    # ---------- ИИ ----------
    def analyze(self, gen: int | None = None) -> None:
        gen, state, folder = self._ctx(gen)
        path = self._text_path(folder)
        if not path.exists() or not state.get("text_chars"):
            raise TenderError("Нет текста ТЗ. Скачайте документы с ЕИС или выберите файлы.")
        text = path.read_text(encoding="utf-8")
        result = tender_ai.analyze_tz(LLM(self._config), text, on_progress=self._progress_for(gen))
        self._commit(gen, analysis=result, matches=[], estimate=None, totals=None)
        count = len(result.get("positions") or [])
        note = " Документ длинный — разобраны первые части." if result.get("truncated") else ""
        if result.get("failed_parts"):
            note += f" Не разобрано частей: {result['failed_parts']} (сбой ИИ)."
        self._publish(f"ТЗ разобрано: позиций {count}.{note}")

    def match(self, gen: int | None = None) -> None:
        gen, state, _ = self._ctx(gen)
        analysis = state.get("analysis")
        if not analysis:
            raise TenderError("Сначала разберите ТЗ.")
        catalog = load_catalog()
        matches = tender_ai.match_equipment(LLM(self._config), analysis, catalog, on_progress=self._progress_for(gen))
        self._commit(gen, matches=matches)
        source = f"из каталога ({len(catalog)} поз.)" if catalog else "по знаниям ИИ — обязательно проверьте"
        self._publish(f"Оборудование подобрано {source}.")

    def estimate(self, gen: int | None = None) -> None:
        from .analyst import estimate_costs

        gen, state, _ = self._ctx(gen)
        if not state.get("analysis"):
            raise TenderError("Сначала разберите ТЗ.")
        self._progress("Считаю себестоимость и маржу…")
        estimate, totals = estimate_costs(LLM(self._config), dict(state["purchase"] or {}), state["analysis"])
        self._commit(gen, estimate=estimate, totals=totals)

    # ---------- документы ----------
    def build_documents(self, gen: int | None = None) -> None:
        gen, state, out = self._ctx(gen)
        purchase = state.get("purchase") or {}
        analysis = state.get("analysis") or {}
        matches = state.get("matches") or []
        if not analysis:
            raise TenderError("Сначала разберите ТЗ.")
        company = self._config.get("company", {}) or {}
        self._progress("Формирую документы…")
        made = [doc_builder.build_checklist(out / "Сводка ТЗ и чек-лист.docx", purchase, analysis, matches)]
        if matches:
            made.append(doc_builder.build_offer(out / "Предложение участника.docx", purchase, analysis, matches, company))
            made.append(doc_builder.build_commercial(out / "Коммерческое предложение.docx", purchase, matches, company))
        self._commit(gen, documents=[str(p) for p in made])
        hint = "" if matches else " Подберите оборудование, чтобы получить предложение участника и КП."
        self._publish(f"Готово документов: {len(made)}.{hint}")

    # ---------- конвейер: всё по очереди ----------
    STEPS = ("files", "analysis", "match", "estimate", "docs")
    STEP_TITLES = {
        "files": "Документы", "analysis": "Разбор ТЗ", "match": "Подбор оборудования",
        "estimate": "Стоимость и маржа", "docs": "Документы заявки",
    }

    def _step(self, name: str, status: str, note: str = "", gen: int | None = None) -> None:
        with self._lock:
            if gen is not None and gen != self._gen:
                raise _Cancelled()
            self.state.setdefault("steps", {})[name] = {"status": status, "note": note}
            self._save()
        self._publish(f"{self.STEP_TITLES[name]}: {note}" if note else "", busy=status == "running" or self._running)

    @staticmethod
    def _error_text(exc: Exception) -> str:
        text = " ".join(str(exc).split())
        return text or f"неожиданная ошибка ({exc.__class__.__name__})"

    def _files_note(self, state: dict[str, Any]) -> str:
        files = state.get("files") or []
        top = [f for f in files if "_zip" not in str(Path(f.get("path", "")).parent.name)]
        with_text = sum(1 for f in files if f.get("chars"))
        bad = [f for f in files if f.get("kind") == "error" or str(f.get("status", "")).startswith("ошибка")]
        note = f"{len(top)} {_plural_files(len(top))}, с текстом: {with_text}"
        if bad:
            note += f"; не прочитаны: {len(bad)} ({bad[0]['name']}: {str(bad[0].get('status', '')).removeprefix('ошибка: ')})"
        errors = state.get("download_errors") or []
        if errors:
            note += f"; не скачались: {len(errors)} ({errors[0]})"
        return note

    def run_pipeline(self, client: ProcurementClient, on_report: Callable[[dict[str, Any]], None] | None = None) -> None:
        """Скачивание → разбор ТЗ → подбор → стоимость → документы. Готовые шаги пропускаются.

        Если во время работы выбрали другую закупку, конвейер останавливается и ничего не пишет в новую заявку.
        """
        with self._lock:
            my_gen = self._gen
        with self._pipeline_lock:
            self._running = True
            try:
                final = self._run_steps(my_gen, client, on_report)
            except _Cancelled:
                return
            finally:
                self._running = False
            if final:
                self._publish(final)  # единственная публикация с busy=False — интерфейс не мигает между шагами

    def _run_steps(self, gen: int, client: ProcurementClient, on_report: Callable[[dict[str, Any]], None] | None) -> str:
        """Шаги конвейера. Возвращает итоговое сообщение для пользователя (публикует его run_pipeline)."""
        _, state, _ = self._ctx(gen)
        if not state.get("purchase"):
            return ""
        failed: list[str] = []
        # 1. документы
        if not state.get("text_chars"):
            self._step("files", "running", "скачиваю с ЕИС…", gen)
            try:
                self.fetch_from_eis(client, gen)
            except _Cancelled:
                raise
            except Exception as exc:
                self._step("files", "error", f"{self._error_text(exc)} Добавьте файлы кнопкой «Выбрать файлы» — разбор продолжится сам.", gen)
                return "Не удалось получить документы — подробности в шаге «Документы»."
            _, state, _ = self._ctx(gen)
            if not state.get("text_chars"):
                self._step("files", "error", f"в файлах нет текста (возможно, сканы): {self._files_note(state)}. "
                                             "Добавьте файлы с текстом кнопкой «Выбрать файлы».", gen)
                return "В документах нет текста — подробности в шаге «Документы»."
        self._step("files", "done", self._files_note(state), gen)
        # 2. разбор ТЗ
        _, state, _ = self._ctx(gen)
        if not state.get("analysis"):
            self._step("analysis", "running", "читаю ТЗ…", gen)
            try:
                self.analyze(gen)
            except _Cancelled:
                raise
            except Exception as exc:
                self._step("analysis", "error", self._error_text(exc), gen)
                return "Разбор ТЗ не удался — подробности в шаге «Разбор ТЗ»."
        _, state, _ = self._ctx(gen)
        analysis = state.get("analysis") or {}
        positions = analysis.get("positions") or []
        note = f"позиций: {len(positions)}"
        if analysis.get("failed_parts"):
            note += f"; не разобрано частей: {analysis['failed_parts']} (сбой ИИ, повторите позже)"
        if analysis.get("truncated"):
            note += "; документ длинный — разобраны первые части"
        if not positions:
            note += " — в тексте не нашлось позиций оборудования, проверьте, что среди файлов есть ТЗ"
        self._step("analysis", "done", note, gen)
        # 3. подбор
        if not positions:
            self._step("match", "skipped", "в ТЗ нет позиций оборудования", gen)
        else:
            if not state.get("matches"):
                self._step("match", "running", "подбираю модели…", gen)
                try:
                    self.match(gen)
                except _Cancelled:
                    raise
                except Exception as exc:
                    self._step("match", "error", self._error_text(exc), gen)
                    failed.append("match")
            _, state, _ = self._ctx(gen)
            matches = state.get("matches") or []
            if matches:
                found = sum(1 for m in matches if m.get("compliance") != "не найдено")
                self._step("match", "done", f"подобрано: {found} из {len(matches)}", gen)
        # 4. стоимость
        _, state, _ = self._ctx(gen)
        if not positions and not str(analysis.get("summary") or analysis.get("object") or "").strip():
            self._step("estimate", "skipped", "нет данных для расчёта: в ТЗ нет позиций", gen)
        else:
            if not state.get("estimate"):
                self._step("estimate", "running", "считаю себестоимость и маржу…", gen)
                try:
                    self.estimate(gen)
                except _Cancelled:
                    raise
                except Exception as exc:
                    self._step("estimate", "error", self._error_text(exc), gen)
                    failed.append("estimate")
            _, state, _ = self._ctx(gen)
            if state.get("estimate"):
                self._step("estimate", "done", self._estimate_note(state.get("totals") or {}), gen)
        # 5. документы Word
        self._step("docs", "running", "формирую черновики…", gen)
        try:
            self.build_documents(gen)
            _, state, _ = self._ctx(gen)
            self._step("docs", "done", f"файлов: {len(state.get('documents') or [])}", gen)
        except _Cancelled:
            raise
        except Exception as exc:
            self._step("docs", "error", self._error_text(exc), gen)
            failed.append("docs")
        _, state, _ = self._ctx(gen)
        if on_report and state.get("analysis"):
            on_report({
                "purchase": dict(state["purchase"]), "analysis": state.get("analysis"),
                "estimate": state.get("estimate"), "totals": state.get("totals"),
                "at": datetime.now().isoformat(timespec="seconds"), "error": "",
                "source": "заявка",
            })
        if failed:
            names = ", ".join(f"«{self.STEP_TITLES[k]}»" for k in failed)
            return f"Готово с ошибками в шагах {names} — причина указана в шаге."
        return "Готово: разбор, подбор, расчёт и документы — ниже."

    @staticmethod
    def _estimate_note(totals: dict[str, Any]) -> str:
        from .procurement import format_money

        if not totals:
            return "готово"
        note = f"себестоимость {format_money(totals.get('cost_min'))} – {format_money(totals.get('cost_max'))}"
        if totals.get("margin_pct_min") is not None:
            note += f", маржа {totals['margin_pct_min']}…{totals['margin_pct_max']}%"
        return note

    def import_catalog(self, path: str) -> int:
        items = tender_ai.load_catalog_xlsx(Path(path))
        catalog_path().write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        self._publish(f"Каталог загружен: {len(items)} позиций.")
        return len(items)
