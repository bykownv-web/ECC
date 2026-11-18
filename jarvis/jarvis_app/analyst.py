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
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import tender_ai, tender_docs
from .config import Config, app_data_dir, documents_dir
from .llm import LLM, LLMError
from .procurement import ProcurementError, Purchase, format_money

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


class AnalystStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or store_path()
        self._lock = threading.Lock()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        self.scores: dict[str, dict[str, Any]] = data.get("scores", {}) if isinstance(data, dict) else {}
        self.reports: list[dict[str, Any]] = data.get("reports", []) if isinstance(data, dict) else []

    def save(self) -> None:
        with self._lock:
            if len(self.scores) > MAX_STORE:
                keep = sorted(self.scores.items(), key=lambda kv: kv[1].get("at", ""))[-MAX_STORE // 2:]
                self.scores = dict(keep)
            self.reports = self.reports[:100]
            self._path.write_text(json.dumps({"scores": self.scores, "reports": self.reports}, ensure_ascii=False), encoding="utf-8")


def compute_totals(estimate: dict[str, Any], nmck: float | None) -> dict[str, Any]:
    """Итоги, маржа и её процент считаются здесь, а не моделью."""
    eq_min = eq_max = 0.0
    for item in estimate.get("equipment") or []:
        qty = float(item.get("quantity") or 0) or 1.0
        lo = float(item.get("unit_cost_min") or 0)
        hi = float(item.get("unit_cost_max") or 0) or lo
        lo, hi = min(lo, hi), max(lo, hi)
        item["total_min"] = round(lo * qty, 2)
        item["total_max"] = round(hi * qty, 2)
        eq_min += lo * qty
        eq_max += hi * qty
    inst_min = float(estimate.get("installation_cost_min") or 0) if estimate.get("installation_required") else 0.0
    inst_max = float(estimate.get("installation_cost_max") or 0) if estimate.get("installation_required") else 0.0
    inst_min, inst_max = min(inst_min, inst_max or inst_min), max(inst_min, inst_max)
    cost_min, cost_max = eq_min + inst_min, eq_max + inst_max
    totals: dict[str, Any] = {
        "equipment_min": round(eq_min, 2), "equipment_max": round(eq_max, 2),
        "installation_min": round(inst_min, 2), "installation_max": round(inst_max, 2),
        "cost_min": round(cost_min, 2), "cost_max": round(cost_max, 2),
        "nmck": nmck, "margin_min": None, "margin_max": None, "margin_pct_min": None, "margin_pct_max": None,
    }
    if nmck:
        totals["margin_min"] = round(nmck - cost_max, 2)
        totals["margin_max"] = round(nmck - cost_min, 2)
        totals["margin_pct_min"] = round((nmck - cost_max) / nmck * 100, 1)
        totals["margin_pct_max"] = round((nmck - cost_min) / nmck * 100, 1)
    return totals


def format_report(report: dict[str, Any]) -> str:
    """Текстовый отчёт для Telegram и почты."""
    p = report["purchase"]
    t = report.get("totals") or {}
    e = report.get("estimate") or {}
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
        if e.get("risks"):
            lines.append("Риски: " + "; ".join(e["risks"]))
        lines.append(f"Рекомендация: {e.get('recommendation', '—').upper()} — {e.get('reasoning', '')}")
    lines.append(p.get("url") or "")
    lines.append("Цены ориентировочные, проверьте перед участием.")
    return "\n".join(line for line in lines if line is not None)


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
        top = sorted(self.store.scores.values(), key=lambda s: -int(s.get("score", 0)))[:40]
        return {"reports": self.store.reports[:30], "scores": top, "settings": self.settings()}

    # ---------- шаг 1: оценка ----------
    def score(self, purchases: list[Purchase]) -> list[dict[str, Any]]:
        profile = self.settings().get("profile", "").strip()
        if not profile:
            raise ValueError("Заполните профиль компании на вкладке «Аналитика»: что вы поставляете и монтируете.")
        new = [p for p in purchases if p.number not in self.store.scores]
        llm = self._llm_factory()
        for i in range(0, len(new), SCORE_BATCH):
            batch = new[i:i + SCORE_BATCH]
            self._progress(f"Оцениваю закупки: {min(i + SCORE_BATCH, len(new))} из {len(new)}…")
            payload = [{"number": p.number, "title": p.title[:300], "customer": p.customer[:120], "nmck": p.price} for p in batch]
            data = llm.json(SCORE_SYSTEM, f"Профиль компании:\n{profile}\n\nЗакупки:\n{json.dumps(payload, ensure_ascii=False)}", SCORE_SCHEMA)
            got = {str(s.get("number")): s for s in data.get("scores") or [] if isinstance(s, dict)}
            stamp = datetime.now().isoformat(timespec="seconds")
            for p in batch:
                s = got.get(p.number)
                if s is None:
                    continue  # модель пропустила — оценим в следующий раз
                self.store.scores[p.number] = {
                    "number": p.number, "score": max(0, min(100, int(s.get("score", 0)))), "reason": str(s.get("reason", ""))[:300],
                    "title": p.title, "customer": p.customer, "price": p.price, "deadline": p.deadline, "law": p.law, "url": p.url, "at": stamp,
                }
        self.store.save()
        by_number = {p.number for p in purchases}
        return sorted((s for n, s in self.store.scores.items() if n in by_number), key=lambda s: -s["score"])

    # ---------- шаг 2: глубокий разбор ----------
    def deep_report(self, scored: dict[str, Any]) -> dict[str, Any]:
        purchase = {k: scored.get(k) for k in ("number", "title", "customer", "price", "deadline", "law", "url")}
        report: dict[str, Any] = {
            "purchase": purchase, "score": scored.get("score"), "score_reason": scored.get("reason", ""),
            "at": datetime.now().isoformat(timespec="seconds"), "analysis": None, "estimate": None, "totals": None, "error": "",
        }
        folder = documents_dir() / "Аналитика" / tender_docs.safe_name(str(purchase["number"]))
        llm = self._llm_factory()
        try:
            self._progress(f"Скачиваю документы № {purchase['number']}…")
            files = tender_docs.fetch_eis_documents(self._client_factory(), purchase["url"], folder / "files")
            _, text = tender_docs.process_files(files, folder / "files")
            if not text.strip():
                raise ValueError("в документах нет текста (возможно, сканы)")
            analysis = tender_ai.analyze_tz(llm, text, on_progress=self._progress)
            report["analysis"] = analysis
            from .tender import load_catalog

            catalog = load_catalog()
            context = {"purchase": purchase, "analysis": analysis}
            if catalog:
                candidates: list[dict[str, Any]] = []
                for pos in analysis.get("positions") or []:
                    candidates.extend(c for c in tender_ai._catalog_candidates(pos, catalog, limit=10) if c not in candidates)
                context["catalog"] = candidates[:60]
            self._progress(f"Считаю стоимость № {purchase['number']}…")
            estimate = llm.json(ESTIMATE_SYSTEM, json.dumps(context, ensure_ascii=False), ESTIMATE_SCHEMA)
            report["estimate"] = estimate
            report["totals"] = compute_totals(estimate, float(purchase["price"]) if purchase.get("price") else None)
        except (ProcurementError, LLMError, ValueError, OSError) as exc:
            report["error"] = str(exc)
        (folder).mkdir(parents=True, exist_ok=True)
        (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        (folder / "Отчёт.txt").write_text(format_report(report), encoding="utf-8")
        return report

    def run(self, purchases: list[Purchase], notify: bool = True) -> list[dict[str, Any]]:
        if not self._lock.acquire(blocking=False):
            raise ValueError("Аналитика уже выполняется.")
        try:
            s = self.settings()
            scored = self.score(purchases)
            done = {r["purchase"]["number"] for r in self.store.reports}
            min_score = int(s.get("min_score", 60))
            todo = [x for x in scored if x["score"] >= min_score and x["number"] not in done][: int(s.get("top_n", 3))]
            new_reports = []
            for item in todo:
                report = self.deep_report(item)
                self.store.reports.insert(0, report)
                self.store.save()
                new_reports.append(report)
                self._emit("analyst_state", self.snapshot())
                if notify and self._notify:
                    self._notify(f"Интересная закупка ({report['score']}/100)", format_report(report))
            self._emit("analyst_state", self.snapshot())
            self._emit("analyst_status", {"text": f"Готово: оценено {len(scored)}, разобрано подробно {len(new_reports)}.", "busy": False})
            return new_reports
        finally:
            self._lock.release()
