"""Разбор технического задания и подбор оборудования с помощью ИИ.

Длинные документы делятся на части; позиции из всех частей объединяются.
Подбор идёт из каталога пользователя (Excel), а без каталога ИИ предлагает модели сам —
такие предложения помечаются «проверить».
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .llm import LLM

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
    return chunks


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").lower()).strip()


def merge_analyses(parts: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {k: "" for k, v in ANALYSIS_SCHEMA["properties"].items() if v.get("type") == "string"}
    for key in ("participant_requirements", "documents_required", "restrictions"):
        merged[key] = []
    merged["positions"] = []
    seen_positions: dict[str, dict[str, Any]] = {}
    for part in parts:
        for key in list(merged):
            if key == "positions":
                continue
            value = part.get(key)
            if isinstance(merged[key], list):
                for item in value or []:
                    if isinstance(item, str) and item.strip() and _norm(item) not in {_norm(x) for x in merged[key]}:
                        merged[key].append(item.strip())
            elif isinstance(value, str) and value.strip() and not merged[key]:
                merged[key] = value.strip()
        for pos in part.get("positions") or []:
            if not isinstance(pos, dict) or not str(pos.get("name", "")).strip():
                continue
            key = _norm(pos["name"])
            chars = [c for c in pos.get("characteristics") or [] if isinstance(c, dict) and c.get("name")]
            if key in seen_positions:
                existing = seen_positions[key]
                names = {_norm(c["name"]) for c in existing["characteristics"]}
                existing["characteristics"].extend(c for c in chars if _norm(c["name"]) not in names)
                for field in ("quantity", "unit", "okpd2"):
                    if not existing.get(field) and pos.get(field):
                        existing[field] = pos[field]
            else:
                item = {
                    "name": str(pos["name"]).strip(),
                    "quantity": str(pos.get("quantity", "")).strip(),
                    "unit": str(pos.get("unit", "")).strip(),
                    "okpd2": str(pos.get("okpd2", "")).strip(),
                    "characteristics": [
                        {"name": str(c["name"]).strip(), "requirement": str(c.get("requirement", "")).strip()}
                        for c in chars
                    ],
                }
                seen_positions[key] = item
                merged["positions"].append(item)
    return merged


def analyze_tz(llm: LLM, text: str, on_progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    size = getattr(llm, "chunk_chars", CHUNK_CHARS)
    chunks = chunk_text(text, size=size)
    if not chunks:
        raise ValueError("В документах нет текста для разбора.")
    if len(chunks) > MAX_CHUNKS:
        chunks = chunks[:MAX_CHUNKS]
    parts = []
    for i, chunk in enumerate(chunks, start=1):
        if on_progress:
            on_progress(f"Разбираю ТЗ: часть {i} из {len(chunks)}…")
        parts.append(llm.json(ANALYSIS_SYSTEM, f"Фрагмент документации ({i}/{len(chunks)}):\n\n{chunk}", ANALYSIS_SCHEMA))
    result = merge_analyses(parts)
    result["truncated"] = len(chunk_text(text, size=size)) > MAX_CHUNKS
    return result


# ---------- каталог ----------
def _catalog_candidates(position: dict[str, Any], catalog: list[dict[str, Any]], limit: int = 40) -> list[dict[str, Any]]:
    """Грубый отбор записей каталога по совпадению слов, чтобы не отправлять ИИ весь прайс."""
    words = {w for w in re.findall(r"[a-zа-яё0-9]{3,}", _norm(position.get("name", ""))) if len(w) > 2}
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
    catalog = catalog or []
    by_id = {str(item["id"]): item for item in catalog}
    results: list[dict[str, Any]] = []
    for index, position in enumerate(positions):
        if on_progress:
            on_progress(f"Подбираю: {position['name'][:60]} ({index + 1} из {len(positions)})…")
        payload = {"position_index": index, "position": position}
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
        match = next((m for m in data.get("matches") or [] if isinstance(m, dict)), None) or {}
        item = {
            "position_index": index,
            "position": position["name"],
            "quantity": position.get("quantity", ""),
            "unit": position.get("unit", ""),
            "brand": str(match.get("brand", "")),
            "model": str(match.get("model", "")),
            "country": str(match.get("country", "")),
            "compliance": match.get("compliance") or "не найдено",
            "price": str(match.get("price", "")),
            "offered_values": [v for v in match.get("offered_values") or [] if isinstance(v, dict)],
            "notes": str(match.get("notes", "")),
            "from_catalog": False,
        }
        catalog_id = str(match.get("catalog_id", "")).strip()
        if catalog and catalog_id in by_id:
            # Данные модели берём из каталога, а не из ответа ИИ, чтобы не было выдуманных моделей и цен.
            src = by_id[catalog_id]
            item.update(
                brand=src.get("brand", "") or item["brand"],
                model=src.get("model", "") or src.get("name", ""),
                country=src.get("country", "") or item["country"],
                price=str(src.get("price", "")) or item["price"],
                from_catalog=True,
            )
        elif catalog:
            item.update(brand="", model="", price="", compliance="не найдено")
            item["notes"] = (item["notes"] + " В каталоге нет подходящей модели.").strip()
        else:
            item["notes"] = ("Проверить по спецификации производителя. " + item["notes"]).strip()
        results.append(item)
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
