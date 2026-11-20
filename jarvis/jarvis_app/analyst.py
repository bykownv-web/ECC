"""Автоаналитика закупок.

1. Быстрая оценка: ИИ по названию закупки ставит баллы 0–100 — насколько она подходит под профиль
   компании (что вы поставляете и монтируете).
2. Глубокий разбор самых подходящих: скачивание документов с ЕИС, разбор ТЗ, состав оборудования,
   ориентировочная стоимость закупки оборудования и монтажа, маржа относительно НМЦК, риски, рекомендация.

Цены — ориентировочные: из каталога поставщика, если он загружен, иначе оценка ИИ (помечается).
Итоги и маржа считаются в коде, а не берутся из ответа ИИ.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import tender_ai, tender_docs
from .config import Config, app_data_dir, documents_dir
from .llm import LLM, LLMError, normalize, to_bool, to_number
from .procurement import ProcurementError, Purchase, format_money, parse_price

SCORE_BATCH = 15
MAX_STORE = 3000

_STR = {"type": "string"}
_NUM = {"type": "number"}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


SCORE_SCHEMA = _obj({"scores": {"type": "array", "items": _obj({"number": _STR, "score": {"type": "integer"}, "reason": _STR})}})
SCORE_SYSTEM = """Ты — аналитик отдела тендеров. Оцени, насколько каждая закупка подходит компании с профилем ниже,
по шкале 0–100: 90–100 — прямо наш профиль (поставка и/или монтаж нашего оборудования), 60–89 — подходит частично
(есть наше оборудование среди прочего), 30–59 — косвенно, 0–29 — не наше (ремонт дорог, продукты, услуги без
оборудования и т.п.). reason — одно короткое предложение по-русски. Верни оценку для КАЖДОЙ закупки, number — как во входе.
Отвечай только JSON по схеме."""

ESTIMATE_SCHEMA = _obj(
    {
        "equipment": {
            "type": "array",
            "items": _obj(
                {
                    "name": _STR,
                    "quantity": _NUM,
                    "unit": _STR,
                    "key_specs": _STR,
                    "unit_cost_min": _NUM,
                    "unit_cost_max": _NUM,
                    "price_basis": _STR,
                }
            ),
        },
        "installation_required": {"type": "boolean"},
        "installation_scope": _STR,
        "installation_cost_min": _NUM,
        "installation_cost_max": _NUM,
        "other_costs": _STR,
        "risks": {"type": "array", "items": _STR},
        "recommendation": {"type": "string", "enum": ["участвовать", "подумать", "не участвовать"]},
        "reasoning": _STR,
    }
)
ESTIMATE_SYSTEM = """Ты — инженер-сметчик и тендерный аналитик компании-поставщика проекторов, мультимедиа, звукового,
светового и театрального оборудования. По разбору ТЗ составь состав оборудования и ориентировочную себестоимость
закупки в рублях (закупочная/дилерская цена в России, а не розница), с диапазоном min–max на единицу.
price_basis: «каталог» — если цена взята из каталога поставщика (используй его, если позиция там есть),
иначе «оценка ИИ». Если ТЗ требует монтаж, пусконаладку, инсталляцию или обучение — installation_required=true,
опиши объём и оцени стоимость работ. Если в ТЗ монтажа нет — false и нули. other_costs — доставка, расходники,
кабели и т.п. словами. risks — короткие пункты (сжатые сроки, ПП 1875/нацрежим, редкое оборудование, штрафы,
обеспечение). recommendation и reasoning — с учётом НМЦК и маржи. Не выдумывай требования, которых нет в ТЗ.
Отвечай только JSON по схеме."""


def store_path() -> Path:
    return app_data_dir() / "analyst.json"


def write_json_atomic(path: Path, data: Any, indent: int | None = None) -> None:
    """Запись через временный файл: при сбое посреди записи старый файл не превращается в обрывок."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
    os.replace(tmp, path)


class AnalystStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or store_path()
        self.lock = threading.RLock()  # оценка, разбор и отчёты из вкладки «Заявка» идут из разных потоков
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            data = {}
        scores = data.get("scores", {}) if isinstance(data, dict) else {}
        reports = data.get("reports", []) if isinstance(data, dict) else []
        self.scores: dict[str, dict[str, Any]] = {str(k): v for k, v in scores.items() if isinstance(v, dict)} if isinstance(scores, dict) else {}
        self.reports: list[dict[str, Any]] = [
            r for r in reports if isinstance(r, dict) and isinstance(r.get("purchase"), dict)
        ] if isinstance(reports, list) else []

    def put_report(self, report: dict[str, Any]) -> None:
        """Новый отчёт — первым; старый отчёт по той же закупке заменяется."""
        number = str(report["purchase"].get("number"))
        with self.lock:
            self.reports = [r for r in self.reports if str(r["purchase"].get("number")) != number]
            self.reports.insert(0, report)
            self.save()

    def save(self) -> None:
        with self.lock:
            if len(self.scores) > MAX_STORE:
                keep = sorted(self.scores.items(), key=lambda kv: str(kv[1].get("at", "")))[-MAX_STORE // 2:]
                self.scores = dict(keep)
            self.reports = self.reports[:100]
            write_json_atomic(self._path, {"scores": self.scores, "reports": self.reports})


def _num(value: Any) -> float:
    """Число из ответа ИИ: 450000, "450 000,00", "≈450 тыс.", None → 0. Отрицательные суммы не имеют смысла."""
    num = to_number(value)
    return max(0.0, num) if num is not None else 0.0


def compute_totals(estimate: dict[str, Any], nmck: Any) -> dict[str, Any]:
    """Итоги, маржа и её процент считаются здесь, а не моделью.

    Числа могут прийти строками («1 234,5», «85%») или пустыми — это не должно ронять расчёт.
    """
    if not isinstance(estimate, dict):
        estimate = {}
    nmck_value = to_number(nmck) if not isinstance(nmck, str) else parse_price(nmck)
    nmck_value = nmck_value if nmck_value and nmck_value > 0 else None
    eq_min = eq_max = 0.0
    equipment = estimate.get("equipment")
    equipment = [e for e in equipment if isinstance(e, dict)] if isinstance(equipment, list) else []
    estimate["equipment"] = equipment
    for item in equipment:
        qty = _num(item.get("quantity")) or 1.0
        lo = _num(item.get("unit_cost_min"))
        hi = _num(item.get("unit_cost_max")) or lo
        lo = lo or hi
        lo, hi = min(lo, hi), max(lo, hi)
        item["total_min"] = round(lo * qty, 2)
        item["total_max"] = round(hi * qty, 2)
        eq_min += lo * qty
        eq_max += hi * qty
    required = to_bool(estimate.get("installation_required"))
    estimate["installation_required"] = required
    inst_min = _num(estimate.get("installation_cost_min")) if required else 0.0
    inst_max = _num(estimate.get("installation_cost_max")) if required else 0.0
    inst_min, inst_max = min(inst_min or inst_max, inst_max or inst_min), max(inst_min, inst_max)
    cost_min, cost_max = eq_min + inst_min, eq_max + inst_max
    totals: dict[str, Any] = {
        "equipment_min": round(eq_min, 2), "equipment_max": round(eq_max, 2),
        "installation_min": round(inst_min, 2), "installation_max": round(inst_max, 2),
        "cost_min": round(cost_min, 2), "cost_max": round(cost_max, 2),
        "nmck": nmck_value, "margin_min": None, "margin_max": None, "margin_pct_min": None, "margin_pct_max": None,
    }
    nmck = nmck_value
    if nmck:
        totals["margin_min"] = round(nmck - cost_max, 2)
        totals["margin_max"] = round(nmck - cost_min, 2)
        totals["margin_pct_min"] = round((nmck - cost_max) / nmck * 100, 1)
        totals["margin_pct_max"] = round((nmck - cost_min) / nmck * 100, 1)
    return totals


def estimate_costs(llm: Any, purchase: dict[str, Any], analysis: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Оценка себестоимости оборудования и монтажа по разбору ТЗ; итоги и маржа — в коде."""
    from .tender import load_catalog

    catalog = load_catalog()
    context: dict[str, Any] = {"purchase": purchase, "analysis": analysis}
    if catalog:
        candidates: list[dict[str, Any]] = []
        for pos in analysis.get("positions") or []:
            candidates.extend(c for c in tender_ai._catalog_candidates(pos, catalog, limit=10) if c not in candidates)
        context["catalog"] = candidates[:60]
    estimate = llm.json(ESTIMATE_SYSTEM, json.dumps(context, ensure_ascii=False, default=str), ESTIMATE_SCHEMA)
    estimate = normalize(ESTIMATE_SCHEMA, estimate if isinstance(estimate, dict) else {})
    return estimate, compute_totals(estimate, purchase.get("price"))


def format_report(report: dict[str, Any]) -> str:
    """Текстовый отчёт для Telegram и почты."""
    p = report.get("purchase") or {}
    t = report.get("totals") or {}
    e = report.get("estimate") or {}
    if not isinstance(e, dict):
        e = {}
    lines = [
        f"{(p.get('title') or '')[:200]}",
        f"№ {p.get('number')} · {p.get('law') or ''} · НМЦК {format_money(p.get('price'))} · подача до {p.get('deadline') or '—'}",
        f"Заказчик: {(p.get('customer') or '—')[:150]}",
        f"Соответствие профилю: {report.get('score', '—')}/100 — {report.get('score_reason') or ''}",
    ]
    if report.get("error"):
        lines.append(f"Глубокий разбор не удался: {report['error']}")
    if e.get("equipment"):
        lines.append("\nОборудование:")
        for item in e["equipment"]:
            lines.append(
                f"• {item.get('name')} — {item.get('quantity') or ''} {item.get('unit') or ''}: "
                f"{format_money(item.get('total_min'))} – {format_money(item.get('total_max'))} ({item.get('price_basis', '')})"
            )
    if e:
        if e.get("installation_required"):
            lines.append(f"\nМонтаж: {e.get('installation_scope', '')} — {format_money(t.get('installation_min'))} – {format_money(t.get('installation_max'))}")
        else:
            lines.append("\nМонтаж: в ТЗ не требуется")
        lines.append(f"Себестоимость: {format_money(t.get('cost_min'))} – {format_money(t.get('cost_max'))}")
        if t.get("margin_min") is not None:
            lines.append(
                f"Маржа к НМЦК: {format_money(t['margin_min'])} – {format_money(t['margin_max'])} "
                f"({t['margin_pct_min']}% – {t['margin_pct_max']}%)"
            )
        risks = [str(r) for r in e.get("risks") or [] if r] if isinstance(e.get("risks"), list) else []
        if risks:
            lines.append("Риски: " + "; ".join(risks))
        lines.append(f"Рекомендация: {str(e.get('recommendation') or '—').upper()} — {e.get('reasoning') or ''}")
    if e.get("note"):
        lines.append(str(e["note"]))
    lines.append(p.get("url") or "")
    lines.append("Цены ориентировочные, проверьте перед участием.")
    return "\n".join(line for line in lines if line)


class AnalystBusy(ValueError):
    """Аналитика уже идёт в другом потоке (ручной запуск во время мониторинга)."""


def _score(value: Any) -> int:
    """Балл 0–100 из ответа ИИ: 85, "85", "85%", "85/100", 85.5 — всё приводится к целому."""
    num = to_number(value)
    return max(0, min(100, int(round(num)))) if num is not None else 0


def _number_key(value: Any) -> str:
    """Номер закупки для сравнения: только цифры, без ведущих нулей (ИИ может вернуть номер числом и потерять «0»)."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value if value is not None else "").strip()
    digits = re.sub(r"\D", "", text)
    return (digits.lstrip("0") or digits) if digits else text.lower()


class Analyst:
    def __init__(self, config: Config, client_factory: Callable[[], Any], emit: Callable[[str, dict[str, Any]], None],
                 notify: Callable[[str, str], Any] | None = None, store: AnalystStore | None = None,
                 llm_factory: Callable[[], Any] | None = None) -> None:
        self._config = config
        self._client_factory = client_factory
        self._emit = emit
        self._notify = notify
        self.store = store or AnalystStore()
        self._llm_factory = llm_factory or (lambda: LLM(config))
        self._lock = threading.Lock()

    def settings(self) -> dict[str, Any]:
        return self._config.get("analyst", {}) or {}

    def _progress(self, text: str) -> None:
        self._emit("analyst_status", {"text": text, "busy": True})

    def snapshot(self) -> dict[str, Any]:
        with self.store.lock:
            top = sorted(self.store.scores.values(), key=lambda s: -_score(s.get("score")))[:40]
            reports = list(self.store.reports[:30])
        return {"reports": reports, "scores": top, "settings": self.settings()}

    # ---------- шаг 1: оценка ----------
    def score(self, purchases: list[Purchase]) -> list[dict[str, Any]]:
        profile = self.settings().get("profile", "").strip()
        if not profile:
            raise ValueError("Заполните профиль компании на вкладке «Аналитика»: что вы поставляете и монтируете.")
        unique: dict[str, Purchase] = {}
        for p in purchases:
            unique.setdefault(p.number, p)
        new = [p for p in unique.values() if p.number not in self.store.scores]
        llm = self._llm_factory() if new else None
        errors: list[str] = []
        for i in range(0, len(new), SCORE_BATCH):
            batch = new[i:i + SCORE_BATCH]
            self._progress(f"Оцениваю закупки: {min(i + SCORE_BATCH, len(new))} из {len(new)}…")
            payload = [{"number": p.number, "title": (p.title or "")[:300], "customer": (p.customer or "")[:120], "nmck": p.price}
                       for p in batch]
            try:
                data = llm.json(SCORE_SYSTEM, f"Профиль компании:\n{profile}\n\nЗакупки:\n{json.dumps(payload, ensure_ascii=False)}",
                                SCORE_SCHEMA)
            except LLMError as exc:  # одна неудачная пачка не отменяет остальные
                errors.append(str(exc))
                continue
            data = normalize(SCORE_SCHEMA, data if isinstance(data, dict) else {})
            items = [s for s in data.get("scores") or [] if isinstance(s, dict)]
            got: dict[str, dict[str, Any]] = {}
            for idx, s in enumerate(items):
                key = _number_key(s.get("number"))
                if not key and len(items) == len(batch):  # номер не указан, но порядок совпадает со входом
                    key = _number_key(batch[idx].number)
                if key:
                    got.setdefault(key, s)
            stamp = datetime.now().isoformat(timespec="seconds")
            with self.store.lock:
                for p in batch:
                    s = got.get(_number_key(p.number))
                    if s is None:
                        continue  # модель пропустила — оценим в следующий раз
                    self.store.scores[p.number] = {
                        "number": p.number, "score": _score(s.get("score")), "reason": str(s.get("reason") or "")[:300],
                        "title": p.title, "customer": p.customer, "price": p.price, "deadline": p.deadline, "law": p.law,
                        "url": p.url, "at": stamp,
                    }
        self.store.save()
        if new and errors and not any(p.number in self.store.scores for p in new):
            raise LLMError(f"ИИ не смог оценить закупки: {errors[0]}")
        with self.store.lock:
            scored = [s for n, s in self.store.scores.items() if n in unique]
        return sorted(scored, key=lambda s: -_score(s.get("score")))

    # ---------- шаг 2: глубокий разбор ----------
    def deep_report(self, scored: dict[str, Any]) -> dict[str, Any]:
        purchase = {k: scored.get(k) for k in ("number", "title", "customer", "price", "deadline", "law", "url")}
        report: dict[str, Any] = {
            "purchase": purchase, "score": scored.get("score"), "score_reason": scored.get("reason", ""),
            "at": datetime.now().isoformat(timespec="seconds"), "analysis": None, "estimate": None, "totals": None, "error": "",
            "files": [], "warnings": [],
        }
        folder = documents_dir() / "Аналитика" / tender_docs.safe_name(str(purchase["number"]))
        step = "скачивание документов"
        try:
            if not purchase.get("url"):
                raise ValueError("у закупки нет ссылки на ЕИС")
            llm = self._llm_factory()
            self._progress(f"Скачиваю документы № {purchase['number']}…")
            files = tender_docs.fetch_eis_documents(self._client_factory(), purchase["url"], folder / "files")
            report["warnings"].extend(getattr(files, "errors", []))
            step = "извлечение текста"
            infos, text = tender_docs.process_files(files, folder / "files")
            report["files"] = [f.to_dict() for f in infos]
            report["warnings"].extend(f"{f.name}: {f.status}" for f in infos if f.kind == "error")
            if not text.strip():
                raise ValueError("в документах нет текста (возможно, сканы)")
            step = "разбор ТЗ"
            analysis = tender_ai.analyze_tz(llm, text, on_progress=self._progress)
            report["analysis"] = analysis
            step = "расчёт стоимости"
            self._progress(f"Считаю стоимость № {purchase['number']}…")
            report["estimate"], report["totals"] = estimate_costs(llm, purchase, analysis)
        except (ProcurementError, LLMError, ValueError, OSError) as exc:
            report["error"] = str(exc)
        except Exception as exc:  # неожиданная ошибка в одной закупке не должна останавливать аналитику
            report["error"] = f"сбой на шаге «{step}» ({exc.__class__.__name__}: {exc})"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            write_json_atomic(folder / "report.json", report, indent=1)
            (folder / "Отчёт.txt").write_text(format_report(report), encoding="utf-8")
        except OSError as exc:
            report["warnings"].append(f"отчёт не сохранён в папку: {exc}")
        return report

    def upsert_report(self, report: dict[str, Any]) -> None:
        """Отчёт из вкладки «Заявка» тоже показываем в «Аналитике» (заменяя старый по той же закупке)."""
        number = report["purchase"].get("number")
        scored = self.store.scores.get(str(number)) or {}
        report.setdefault("score", scored.get("score"))
        report.setdefault("score_reason", scored.get("reason", ""))
        self.store.put_report(report)
        self._emit("analyst_state", self.snapshot())

    def run(self, purchases: list[Purchase], notify: bool = True) -> list[dict[str, Any]]:
        if not self._lock.acquire(blocking=False):
            raise AnalystBusy("Аналитика уже выполняется.")
        try:
            s = self.settings()
            scored = self.score(purchases)
            with self.store.lock:
                # закупки с удачным отчётом не разбираем повторно; неудачные (ЕИС была недоступна и т.п.) — пробуем снова
                done = {str(r["purchase"].get("number")) for r in self.store.reports if not r.get("error")}
            min_score = _score(s.get("min_score", 60))
            top_n = max(1, int(to_number(s.get("top_n", 3)) or 3))
            todo = [x for x in scored if _score(x.get("score")) >= min_score and str(x["number"]) not in done][:top_n]
            new_reports = []
            for item in todo:
                report = self.deep_report(item)
                self.store.put_report(report)
                new_reports.append(report)
                self._emit("analyst_state", self.snapshot())
                if notify and self._notify and not report.get("error"):
                    self._notify(f"Интересная закупка ({report['score']}/100)", format_report(report))
            failed = sum(1 for r in new_reports if r.get("error"))
            tail = f", с ошибкой {failed} (причина — в карточке отчёта)" if failed else ""
            self._emit("analyst_state", self.snapshot())
            self._emit("analyst_status", {"text": f"Готово: оценено {len(scored)}, разобрано подробно {len(new_reports)}{tail}.", "busy": False})
            return new_reports
        except Exception as exc:
            self._emit("analyst_status", {"text": str(exc) or exc.__class__.__name__, "busy": False, "error": True})
            raise
        finally:
            self._lock.release()
