"""Разбор технического задания и подбор оборудования с помощью ИИ.

Длинные документы делятся на части; позиции из всех частей объединяются.
Подбор идёт из каталога пользователя (Excel), а без каталога ИИ предлагает модели сам —
такие предложения помечаются «проверить».
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .llm import LLM, LLMError, normalize

CHUNK_CHARS = 12000
MAX_CHUNKS = 12

_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": {"type": "string"}}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


ANALYSIS_SCHEMA = _obj(
    {
        "summary": _STR,
        "object": _STR,
        "delivery_place": _STR,
        "delivery_terms": _STR,
        "deadline": _STR,
        "contract_term": _STR,
        "payment_terms": _STR,
        "acceptance_terms": _STR,
        "installation": _STR,
        "penalties": _STR,
        "bid_security": _STR,
        "contract_security": _STR,
        "warranty": _STR,
        "participant_requirements": _STR_LIST,
        "documents_required": _STR_LIST,
        "restrictions": _STR_LIST,
        "positions": {
            "type": "array",
            "items": _obj(
                {
                    "name": _STR,
                    "quantity": _STR,
                    "unit": _STR,
                    "okpd2": _STR,
                    "characteristics": {
                        "type": "array",
                        "items": _obj({"name": _STR, "requirement": _STR}),
                    },
                }
            ),
        },
    }
)

ANALYSIS_SYSTEM = """Ты — специалист по госзакупкам (44-ФЗ, 223-ФЗ) и подготовке заявок на поставку
проекторов, мультимедийного, звукового и театрального оборудования. Тебе дают фрагмент документации
закупки (ТЗ, извещение, проект контракта). Извлеки факты строго из текста, ничего не придумывай.
Если сведений нет во фрагменте — оставь пустую строку или пустой список.
positions — каждая поставляемая позиция товара с количеством и ВСЕМИ требуемыми характеристиками
в формулировке заказчика (например «не менее 5000 лм», «HDMI — наличие»).
restrictions — запреты и ограничения (ПП РФ № 1875, нацрежим, СМП/СОНО и т.п.).
delivery_terms — срок поставки (сколько дней, с какого момента, этапы); delivery_place — адрес;
contract_term — срок действия контракта; payment_terms — срок и порядок оплаты; acceptance_terms — приёмка и
экспертиза; installation — требуются ли монтаж, пусконаладка, обучение и что именно; penalties — штрафы и пени.
Отвечай только JSON по схеме."""

MATCH_SCHEMA = _obj(
    {
        "matches": {
            "type": "array",
            "items": _obj(
                {
                    "position_index": {"type": "integer"},
                    "catalog_id": _STR,
                    "brand": _STR,
                    "model": _STR,
                    "country": _STR,
                    "compliance": {"type": "string", "enum": ["соответствует", "частично", "не найдено"]},
                    "price": _STR,
                    "offered_values": {
                        "type": "array",
                        "items": _obj({"name": _STR, "value": _STR}),
                    },
                    "notes": _STR,
                }
            ),
        }
    }
)

MATCH_SYSTEM_CATALOG = """Ты подбираешь оборудование под требования госзакупки ТОЛЬКО из каталога поставщика.
Для каждой позиции выбери одну запись каталога, характеристики которой соответствуют требованиям,
и укажи её catalog_id ровно как в каталоге. Если подходящей нет — compliance «не найдено», catalog_id пустой.
offered_values — конкретные значения характеристик предлагаемого товара по каждому требованию заказчика,
взятые из каталога; если значения в каталоге нет — напиши «уточнить». В notes кратко укажи, чего не хватает.
Отвечай только JSON по схеме."""

MATCH_SYSTEM_FREE = """Ты подбираешь реальные модели оборудования (проекторы, мультимедиа, звук, свет, сцена)
под требования госзакупки. Для каждой позиции предложи одну распространённую модель, которая, по твоим
знаниям, соответствует требованиям. Учитывай ограничения на страну происхождения, если они есть.
Ты можешь ошибаться в характеристиках, поэтому в notes всегда пиши, что именно нужно проверить
по официальной спецификации производителя. catalog_id оставь пустым, price — пустым.
offered_values — конкретные значения по каждому требованию заказчика. Отвечай только JSON по схеме."""


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Режет текст по абзацам на части не длиннее size символов."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for para in text.split("\n"):
        if length + len(para) > size and current:
            chunks.append("\n".join(current))
            current, length = [], 0
        while len(para) > size:
            chunks.append(para[:size])
            para = para[size:]
        current.append(para)
        length += len(para) + 1
    if current and "".join(current).strip():
        chunks.append("\n".join(current))
    return [c for c in chunks if c.strip()]


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "").lower()).strip()


def _s(value: Any) -> str:
    """Значение поля как строка: GigaChat иногда присылает числа, списки или null вместо строки."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, list):
        return "; ".join(x for x in (_s(v) for v in value) if x)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_s(v)}" for k, v in value.items() if _s(v))
    return str(value).strip()


def merge_analyses(parts: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {k: "" for k, v in ANALYSIS_SCHEMA["properties"].items() if v.get("type") == "string"}
    for key in ("participant_requirements", "documents_required", "restrictions"):
        merged[key] = []
    merged["positions"] = []
    seen_positions: dict[str, dict[str, Any]] = {}
    for part in parts:
        if not isinstance(part, dict):
            continue
        for key in list(merged):
            if key == "positions":
                continue
            value = part.get(key)
            if isinstance(merged[key], list):
                values = value if isinstance(value, list) else ([value] if value else [])
                for item in values:
                    text = _s(item)
                    if text and _norm(text) not in {_norm(x) for x in merged[key]}:
                        merged[key].append(text)
            else:
                text = _s(value)
                if text and not merged[key]:
                    merged[key] = text
        positions = part.get("positions")
        for pos in positions if isinstance(positions, list) else []:
            if not isinstance(pos, dict) or not _s(pos.get("name")):
                continue
            key = _norm(_s(pos["name"]))
            raw_chars = pos.get("characteristics")
            chars = [
                {"name": _s(c.get("name")), "requirement": _s(c.get("requirement"))}
                for c in (raw_chars if isinstance(raw_chars, list) else [])
                if isinstance(c, dict) and _s(c.get("name"))
            ]
            if key in seen_positions:
                existing = seen_positions[key]
                names = {_norm(c["name"]) for c in existing["characteristics"]}
                for c in chars:
                    if _norm(c["name"]) not in names:
                        existing["characteristics"].append(c)
                        names.add(_norm(c["name"]))
                for field in ("quantity", "unit", "okpd2"):
                    if not existing.get(field) and _s(pos.get(field)):
                        existing[field] = _s(pos.get(field))
            else:
                unique_chars: list[dict[str, str]] = []
                for c in chars:
                    if _norm(c["name"]) not in {_norm(x["name"]) for x in unique_chars}:
                        unique_chars.append(c)
                item = {
                    "name": _s(pos["name"]),
                    "quantity": _s(pos.get("quantity")),
                    "unit": _s(pos.get("unit")),
                    "okpd2": _s(pos.get("okpd2")),
                    "characteristics": unique_chars,
                }
                seen_positions[key] = item
                merged["positions"].append(item)
    return merged


def analyze_tz(llm: LLM, text: str, on_progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Разбор ТЗ по частям. Если часть не разобралась (сбой ИИ) — остальные всё равно идут в итог.

    Ошибка — только когда не разобралась ни одна часть. failed_parts — сколько частей пропущено.
    """
    size = int(getattr(llm, "chunk_chars", CHUNK_CHARS) or CHUNK_CHARS)
    all_chunks = chunk_text(text or "", size=size)
    if not all_chunks:
        raise ValueError("В документах нет текста для разбора.")
    chunks = all_chunks[:MAX_CHUNKS]
    parts: list[dict[str, Any]] = []
    failures: list[str] = []
    first_error: Exception | None = None
    for i, chunk in enumerate(chunks, start=1):
        if on_progress:
            on_progress(f"Разбираю ТЗ: часть {i} из {len(chunks)}…")
        try:
            data = llm.json(ANALYSIS_SYSTEM, f"Фрагмент документации ({i}/{len(chunks)}):\n\n{chunk}", ANALYSIS_SCHEMA)
        except LLMError as exc:
            first_error = first_error or exc
            failures.append(f"часть {i}: {exc}")
            continue
        parts.append(normalize(ANALYSIS_SCHEMA, data))
    if not parts:
        raise first_error or LLMError("ИИ не разобрал ни одной части ТЗ.")
    result = merge_analyses(parts)
    result["truncated"] = len(all_chunks) > MAX_CHUNKS
    result["failed_parts"] = len(failures)
    result["errors"] = failures
    return result


# ---------- каталог ----------
def _catalog_candidates(position: dict[str, Any], catalog: list[dict[str, Any]], limit: int = 40) -> list[dict[str, Any]]:
    """Грубый отбор записей каталога по совпадению слов, чтобы не отправлять ИИ весь прайс."""
    words = {w for w in re.findall(r"[a-zа-яё0-9]{3,}", _norm(position.get("name", ""))) if len(w) > 2}
    catalog = [c for c in catalog if isinstance(c, dict)]
    scored = []
    for item in catalog:
        hay = _norm(" ".join(str(item.get(k, "")) for k in ("name", "category", "brand", "model", "specs")))
        score = sum(1 for w in words if w[:5] in hay)
        if score:
            scored.append((score, item))
    scored.sort(key=lambda x: -x[0])
    return [item for _, item in scored[:limit]] or catalog[:limit]


def match_equipment(
    llm: LLM,
    analysis: dict[str, Any],
    catalog: list[dict[str, Any]] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    positions = analysis.get("positions") or []
    if not positions:
        raise ValueError("В ТЗ не найдено позиций оборудования. Сначала разберите ТЗ.")
    catalog = [c for c in catalog or [] if isinstance(c, dict)]
    by_id = {str(item.get("id", i + 1)): item for i, item in enumerate(catalog)}
    results: list[dict[str, Any]] = []
    failures = 0
    first_error: Exception | None = None
    for index, position in enumerate(positions):
        if not isinstance(position, dict):
            continue
        name = _s(position.get("name")) or f"Позиция {index + 1}"
        if on_progress:
            on_progress(f"Подбираю: {name[:60]} ({index + 1} из {len(positions)})…")
        payload = {"position_index": index, "position": position}
        try:
            if catalog:
                candidates = _catalog_candidates(position, catalog)
                prompt = (
                    "Требования заказчика:\n" + json.dumps(payload, ensure_ascii=False, indent=1)
                    + "\n\nКаталог поставщика (выбирай только отсюда):\n"
                    + json.dumps(candidates, ensure_ascii=False, indent=1)
                )
                data = llm.json(MATCH_SYSTEM_CATALOG, prompt, MATCH_SCHEMA)
            else:
                prompt = "Требования заказчика:\n" + json.dumps(payload, ensure_ascii=False, indent=1)
                data = llm.json(MATCH_SYSTEM_FREE, prompt, MATCH_SCHEMA)
            error_note = ""
        except LLMError as exc:  # сбой на одной позиции не отменяет подбор остальных
            failures += 1
            first_error = first_error or exc
            data, error_note = {}, f"ИИ не ответил: {exc}"
        data = normalize(MATCH_SCHEMA, data if isinstance(data, dict) else {})
        found = [m for m in data.get("matches") or [] if isinstance(m, dict)]
        match = next((m for m in found if m.get("position_index") == index), None) or (found[0] if found else {})
        item = {
            "position_index": index,
            "position": name,
            "quantity": _s(position.get("quantity")),
            "unit": _s(position.get("unit")),
            "brand": _s(match.get("brand")),
            "model": _s(match.get("model")),
            "country": _s(match.get("country")),
            "compliance": match.get("compliance") or "не найдено",
            "price": _s(match.get("price")),
            "offered_values": [
                {"name": _s(v.get("name")), "value": _s(v.get("value"))}
                for v in match.get("offered_values") or [] if isinstance(v, dict)
            ],
            "notes": (_s(match.get("notes")) + (" " + error_note if error_note else "")).strip(),
            "from_catalog": False,
        }
        if item["compliance"] not in ("соответствует", "частично", "не найдено"):
            item["compliance"] = "частично" if item["model"] else "не найдено"
        catalog_id = str(match.get("catalog_id", "")).strip()
        if catalog and catalog_id in by_id:
            # Данные модели берём из каталога, а не из ответа ИИ, чтобы не было выдуманных моделей и цен.
            src = by_id[catalog_id]
            item.update(
                brand=_s(src.get("brand")) or item["brand"],
                model=_s(src.get("model")) or _s(src.get("name")),
                country=_s(src.get("country")) or item["country"],
                price=_s(src.get("price")) or item["price"],
                from_catalog=True,
            )
        elif catalog:
            item.update(brand="", model="", price="", compliance="не найдено")
            item["notes"] = (item["notes"] + " В каталоге нет подходящей модели.").strip()
        else:
            item["notes"] = ("Проверить по спецификации производителя. " + item["notes"]).strip()
        results.append(item)
    if results and failures == len(results) and first_error is not None:
        raise first_error  # ИИ не ответил ни разу — показываем причину, а не пустой подбор
    return results


CATALOG_COLUMNS = {
    "name": ("наименование", "название", "товар", "name"),
    "brand": ("бренд", "производитель", "марка", "brand", "vendor"),
    "model": ("модель", "артикул", "model", "sku"),
    "category": ("категория", "группа", "тип", "category"),
    "specs": ("характеристики", "описание", "параметры", "specs", "description"),
    "price": ("цена", "стоимость", "price"),
    "country": ("страна", "country", "происхождение"),
}


def load_catalog_xlsx(path) -> list[dict[str, Any]]:
    """Читает прайс/каталог из Excel. Заголовки ищутся в первых 10 строках."""
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    header_idx, mapping = -1, {}
    for i, row in enumerate(rows[:10]):
        cells = [_norm(str(c)) if c is not None else "" for c in row]
        found = {}
        for field, keys in CATALOG_COLUMNS.items():
            for col, cell in enumerate(cells):
                if cell and any(cell.startswith(k) or k in cell for k in keys) and col not in found.values():
                    found[field] = col
                    break
        if ("name" in found or "model" in found) and len(found) >= 2:
            header_idx, mapping = i, found
            break
    if header_idx < 0:
        raise ValueError("Не нашёл заголовки. Нужны колонки «Наименование» или «Модель» и, например, «Цена».")
    items = []
    for row in rows[header_idx + 1:]:
        item = {field: (str(row[col]).strip() if col < len(row) and row[col] is not None else "") for field, col in mapping.items()}
        if not (item.get("name") or item.get("model")):
            continue
        item["id"] = str(len(items) + 1)
        items.append(item)
    if not items:
        raise ValueError("В каталоге нет строк с товарами.")
    return items
