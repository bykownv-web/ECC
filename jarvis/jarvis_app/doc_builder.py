"""Черновики документов заявки в формате Word (.docx).

Это заготовки для проверки и доработки человеком: ИИ может ошибиться в характеристиках,
а формы заявки на электронных площадках часто заполняются прямо на площадке.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt, RGBColor

WARNING = (
    "Черновик подготовлен автоматически помощником J.A.R.V.I.S. Перед подачей сверьте каждую "
    "характеристику с документацией закупки и спецификацией производителя."
)


def _s(value: Any) -> str:
    """Строка без «None»: ИИ и старые файлы состояния иногда присылают null вместо пустой строки."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _doc() -> Document:
    doc = Document()
    section = doc.sections[0]
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(1.5)
    section.top_margin = Cm(2)
    section.bottom_margin = Cm(2)
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)
    return doc


def _title(doc: Document, text: str) -> None:
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(14)


def _note(doc: Document, text: str = WARNING) -> None:
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.italic = True
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x88, 0x88, 0x88)


def _table(doc: Document, headers: list[str], rows: list[list[str]], widths: list[float] | None = None):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, header in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.text = ""
        run = cell.paragraphs[0].add_run(header)
        run.bold = True
        run.font.size = Pt(10)
    for row in rows:
        cells = table.add_row().cells
        for i, value in enumerate(list(row)[: len(headers)]):
            cells[i].text = ""
            run = cells[i].paragraphs[0].add_run(_s(value))
            run.font.size = Pt(10)
    if widths:
        for row in table.rows:
            for i, width in enumerate(widths):
                row.cells[i].width = Cm(width)
    return table


def _company_block(doc: Document, company: dict[str, str] | None) -> None:
    c = {k: _s(v) for k, v in _dict(company).items()}
    ids = ", ".join(x for x in (f"ИНН {c['inn']}" if c.get("inn") else "", f"КПП {c['kpp']}" if c.get("kpp") else "") if x)
    lines = [
        c.get("name", ""),
        ids,
        f"ОГРН {c['ogrn']}" if c.get("ogrn") else "",
        c.get("address", ""),
        " · ".join(x for x in (c.get("phone", ""), c.get("email", "")) if x),
    ]
    for line in lines:
        if line.strip():
            p = doc.add_paragraph(line)
            p.paragraph_format.space_after = Pt(0)


def _signature(doc: Document, company: dict[str, str] | None) -> None:
    company = _dict(company)
    doc.add_paragraph()
    position = _s(company.get("director_position")) or "Генеральный директор"
    director = _s(company.get("director")) or "____________________"
    doc.add_paragraph(f"{position}   ____________________ / {director} /")
    doc.add_paragraph("М.П.          «___» ____________ 20___ г.")


def _purchase_line(purchase: dict[str, Any]) -> str:
    purchase = _dict(purchase)
    law = _s(purchase.get("law"))
    return f"Закупка № {_s(purchase.get('number'))}" + (f" ({law})" if law else "") + f": {_s(purchase.get('title'))}"


def _trademark(m: dict[str, Any]) -> str:
    return " ".join(x for x in (_s(m.get("brand")), _s(m.get("model"))) if x)


def _qty_text(m: dict[str, Any]) -> str:
    return f"{_s(m.get('quantity'))} {_s(m.get('unit'))}".strip()


def build_offer(path: Path, purchase: dict[str, Any], analysis: dict[str, Any], matches: list[dict[str, Any]],
                company: dict[str, str]) -> Path:
    """Предложение участника в отношении объекта закупки (характеристики предлагаемого товара)."""
    purchase = _dict(purchase)
    doc = _doc()
    _company_block(doc, company)
    doc.add_paragraph()
    _title(doc, "ПРЕДЛОЖЕНИЕ УЧАСТНИКА ЗАКУПКИ\nв отношении объекта закупки")
    doc.add_paragraph(_purchase_line(purchase))
    doc.add_paragraph(f"Заказчик: {_s(purchase.get('customer'))}")
    positions = [p for p in _items(_dict(analysis).get("positions")) if isinstance(p, dict)]
    rows = []
    for n, m in enumerate((m for m in _items(matches) if isinstance(m, dict)), start=1):
        try:
            idx = int(m.get("position_index"))
        except (TypeError, ValueError):
            idx = -1
        position = positions[idx] if 0 <= idx < len(positions) else {}
        offered = {
            _s(v.get("name")).lower(): _s(v.get("value"))
            for v in _items(m.get("offered_values")) if isinstance(v, dict) and _s(v.get("name"))
        }
        chars = []
        for c in _items(position.get("characteristics")):
            name = _s(c.get("name")) if isinstance(c, dict) else _s(c)
            if name:
                chars.append(f"{name}: {offered.get(name.lower()) or 'уточнить'}")
        rows.append([
            str(idx + 1 if idx >= 0 else n), _s(m.get("position")) or _s(position.get("name")), _trademark(m) or "—",
            "\n".join(chars) or "—", _s(m.get("country")) or "уточнить", _qty_text(m),
        ])
    _table(doc, ["№", "Наименование товара", "Товарный знак, модель", "Характеристики предлагаемого товара",
                 "Страна происхождения", "Кол-во"], rows, [0.8, 3.5, 3, 6.5, 2.3, 1.6])
    _signature(doc, company)
    _note(doc)
    doc.save(str(path))
    return path


_NUMBER_RE = re.compile(r"\d[\d\s\u00a0\u202f.,]*")


def _parse_number(value: Any) -> float | None:
    """Первое число из строки: «450 000,00 ₽», «1,234.56», «1.234.567,89», «12 500 руб.», «3,5»."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER_RE.search(str(value or ""))
    if not match:
        return None
    token = re.sub(r"[\s\u00a0\u202f]", "", match.group(0)).rstrip(".,")
    if "," in token and "." in token:
        decimal = "," if token.rfind(",") > token.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        token = token.replace(thousands, "").replace(decimal, ".")
    elif "," in token or "." in token:
        sep = "," if "," in token else "."
        parts = token.split(sep)
        if len(parts) > 2 or len(parts[-1]) == 3:
            token = "".join(parts)  # 1.234.567 или 1,500 — разделители тысяч
        else:
            token = ".".join(parts)
    try:
        return float(token)
    except ValueError:
        return None


def _price_number(value: Any) -> float | None:
    return _parse_number(value)


def _qty_number(value: Any) -> float:
    number = _parse_number(value)
    return number if number is not None else 1.0


def _money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def build_commercial(path: Path, purchase: dict[str, Any], matches: list[dict[str, Any]], company: dict[str, str]) -> Path:
    purchase = _dict(purchase)
    doc = _doc()
    _company_block(doc, company)
    doc.add_paragraph()
    _title(doc, "КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ")
    doc.add_paragraph(f"Кому: {_s(purchase.get('customer'))}")
    doc.add_paragraph(_purchase_line(purchase))
    rows, total = [], 0.0
    for i, m in enumerate((m for m in _items(matches) if isinstance(m, dict)), start=1):
        price = _price_number(m.get("price"))
        qty = _qty_number(m.get("quantity"))
        amount = price * qty if price is not None else None
        if amount is not None:
            total += amount
        rows.append([
            str(i), f"{_s(m.get('position'))}\n{_trademark(m)}".strip(), _qty_text(m),
            _money(price) if price is not None else "уточнить", _money(amount) if amount is not None else "—",
        ])
    _table(doc, ["№", "Наименование, модель", "Кол-во", "Цена за ед., ₽", "Сумма, ₽"], rows, [0.8, 8, 2, 3, 3])
    p = doc.add_paragraph()
    p.add_run(f"Итого: {_money(total)} ₽").bold = True
    nmck = _price_number(purchase.get("price"))
    if nmck:
        line = f"НМЦК закупки: {_money(nmck)} ₽"
        if total:
            diff = (1 - total / nmck) * 100
            line += f", снижение {diff:.1f}%" if diff >= 0 else f", превышение НМЦК на {-diff:.1f}%"
        doc.add_paragraph(line)
    doc.add_paragraph("Цена включает стоимость товара, доставки, разгрузки, налогов и обязательных платежей (уточнить НДС).")
    doc.add_paragraph(f"Предложение действительно 30 дней с {date.today():%d.%m.%Y}.")
    _signature(doc, company)
    _note(doc)
    doc.save(str(path))
    return path


def build_checklist(path: Path, purchase: dict[str, Any], analysis: dict[str, Any], matches: list[dict[str, Any]]) -> Path:
    purchase = _dict(purchase)
    analysis = _dict(analysis)
    doc = _doc()
    _title(doc, "СВОДКА ТЗ И ЧЕК-ЛИСТ ЗАЯВКИ")
    doc.add_paragraph(_purchase_line(purchase))
    doc.add_paragraph(f"Заказчик: {_s(purchase.get('customer'))}")
    if _s(purchase.get("url")):
        doc.add_paragraph(f"Карточка в ЕИС: {_s(purchase.get('url'))}")
    facts = [
        ("Кратко", analysis.get("summary", "")),
        ("Срок подачи заявок", purchase.get("deadline", "") or analysis.get("deadline", "")),
        ("Место поставки", analysis.get("delivery_place", "")),
        ("Сроки поставки", analysis.get("delivery_terms", "")),
        ("Обеспечение заявки", analysis.get("bid_security", "")),
        ("Обеспечение контракта", analysis.get("contract_security", "")),
        ("Гарантия", analysis.get("warranty", "")),
    ]
    _table(doc, ["Параметр", "Значение"], [[k, _s(v) or "не найдено в документах"] for k, v in facts], [5, 12])
    for title, key in (("Ограничения и запреты", "restrictions"), ("Требования к участнику", "participant_requirements"),
                       ("Документы в составе заявки", "documents_required")):
        doc.add_paragraph()
        doc.add_paragraph().add_run(title).bold = True
        items = [_s(x) for x in _items(analysis.get(key)) if _s(x)]  # строка вместо списка — один пункт, а не по буквам
        for item in items or ["не найдено в документах"]:
            doc.add_paragraph(f"☐ {item}")
    doc.add_paragraph()
    doc.add_paragraph().add_run("Подбор оборудования").bold = True
    rows = [[_s(m.get("position")), _trademark(m) or "—", _s(m.get("compliance")), _s(m.get("notes"))]
            for m in _items(matches) if isinstance(m, dict)]
    _table(doc, ["Позиция", "Предложение", "Соответствие", "Что проверить"], rows, [5, 4, 2.5, 5.5])
    _note(doc)
    doc.save(str(path))
    return path
