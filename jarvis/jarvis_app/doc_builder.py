"""Черновики документов заявки в формате Word (.docx).

Это заготовки для проверки и доработки человеком: ИИ может ошибиться в характеристиках,
а формы заявки на электронных площадках часто заполняются прямо на площадке.
"""

from __future__ import annotations

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
        for i, value in enumerate(row):
            cells[i].text = ""
            run = cells[i].paragraphs[0].add_run(str(value or ""))
            run.font.size = Pt(10)
    if widths:
        for row in table.rows:
            for i, width in enumerate(widths):
                row.cells[i].width = Cm(width)
    return table


def _company_block(doc: Document, company: dict[str, str]) -> None:
    lines = [
        company.get("name", ""),
        f"ИНН {company.get('inn', '')}" + (f", КПП {company['kpp']}" if company.get("kpp") else ""),
        f"ОГРН {company['ogrn']}" if company.get("ogrn") else "",
        company.get("address", ""),
        " · ".join(x for x in (company.get("phone", ""), company.get("email", "")) if x),
    ]
    for line in lines:
        if line.strip() and line.strip() != "ИНН":
            p = doc.add_paragraph(line)
            p.paragraph_format.space_after = Pt(0)


def _signature(doc: Document, company: dict[str, str]) -> None:
    doc.add_paragraph()
    position = company.get("director_position") or "Генеральный директор"
    director = company.get("director") or "____________________"
    doc.add_paragraph(f"{position}   ____________________ / {director} /")
    doc.add_paragraph("М.П.          «___» ____________ 20___ г.")


def _purchase_line(purchase: dict[str, Any]) -> str:
    return f"Закупка № {purchase.get('number', '')} ({purchase.get('law', '')}): {purchase.get('title', '')}"


def build_offer(path: Path, purchase: dict[str, Any], analysis: dict[str, Any], matches: list[dict[str, Any]],
                company: dict[str, str]) -> Path:
    """Предложение участника в отношении объекта закупки (характеристики предлагаемого товара)."""
    doc = _doc()
    _company_block(doc, company)
    doc.add_paragraph()
    _title(doc, "ПРЕДЛОЖЕНИЕ УЧАСТНИКА ЗАКУПКИ\nв отношении объекта закупки")
    doc.add_paragraph(_purchase_line(purchase))
    doc.add_paragraph(f"Заказчик: {purchase.get('customer', '')}")
    positions = analysis.get("positions") or []
    rows = []
    for m in matches:
        idx = m.get("position_index", 0)
        position = positions[idx] if idx < len(positions) else {"characteristics": []}
        offered = {v.get("name", "").lower(): v.get("value", "") for v in m.get("offered_values") or []}
        chars = []
        for c in position.get("characteristics") or []:
            value = offered.get(c["name"].lower(), "уточнить")
            chars.append(f"{c['name']}: {value}")
        trademark = " ".join(x for x in (m.get("brand", ""), m.get("model", "")) if x) or "—"
        rows.append([
            str(idx + 1), m.get("position", ""), trademark, "\n".join(chars) or "—",
            m.get("country", "") or "уточнить", f"{m.get('quantity', '')} {m.get('unit', '')}".strip(),
        ])
    _table(doc, ["№", "Наименование товара", "Товарный знак, модель", "Характеристики предлагаемого товара",
                 "Страна происхождения", "Кол-во"], rows, [0.8, 3.5, 3, 6.5, 2.3, 1.6])
    _signature(doc, company)
    _note(doc)
    doc.save(str(path))
    return path


def _price_number(value: str) -> float | None:
    cleaned = "".join(ch for ch in str(value or "") if ch.isdigit() or ch in ",.")
    if not cleaned:
        return None
    try:
        return float(cleaned.replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _qty_number(value: str) -> float:
    cleaned = "".join(ch for ch in str(value or "") if ch.isdigit() or ch in ",.").replace(",", ".")
    try:
        return float(cleaned) if cleaned else 1.0
    except ValueError:
        return 1.0


def _money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def build_commercial(path: Path, purchase: dict[str, Any], matches: list[dict[str, Any]], company: dict[str, str]) -> Path:
    doc = _doc()
    _company_block(doc, company)
    doc.add_paragraph()
    _title(doc, "КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ")
    doc.add_paragraph(f"Кому: {purchase.get('customer', '')}")
    doc.add_paragraph(_purchase_line(purchase))
    rows, total = [], 0.0
    for i, m in enumerate(matches, start=1):
        price = _price_number(m.get("price", ""))
        qty = _qty_number(m.get("quantity", ""))
        amount = price * qty if price is not None else None
        if amount is not None:
            total += amount
        name = m.get("position", "")
        model = " ".join(x for x in (m.get("brand", ""), m.get("model", "")) if x)
        rows.append([
            str(i), f"{name}\n{model}".strip(), f"{m.get('quantity', '')} {m.get('unit', '')}".strip(),
            _money(price) if price is not None else "уточнить", _money(amount) if amount is not None else "—",
        ])
    _table(doc, ["№", "Наименование, модель", "Кол-во", "Цена за ед., ₽", "Сумма, ₽"], rows, [0.8, 8, 2, 3, 3])
    p = doc.add_paragraph()
    p.add_run(f"Итого: {_money(total)} ₽").bold = True
    nmck = purchase.get("price")
    if isinstance(nmck, (int, float)) and nmck:
        diff = (1 - total / nmck) * 100 if total else 0
        doc.add_paragraph(f"НМЦК закупки: {_money(float(nmck))} ₽" + (f", снижение {diff:.1f}%" if total else ""))
    doc.add_paragraph("Цена включает стоимость товара, доставки, разгрузки, налогов и обязательных платежей (уточнить НДС).")
    doc.add_paragraph(f"Предложение действительно 30 дней с {date.today():%d.%m.%Y}.")
    _signature(doc, company)
    _note(doc)
    doc.save(str(path))
    return path


def build_checklist(path: Path, purchase: dict[str, Any], analysis: dict[str, Any], matches: list[dict[str, Any]]) -> Path:
    doc = _doc()
    _title(doc, "СВОДКА ТЗ И ЧЕК-ЛИСТ ЗАЯВКИ")
    doc.add_paragraph(_purchase_line(purchase))
    doc.add_paragraph(f"Заказчик: {purchase.get('customer', '')}")
    if purchase.get("url"):
        doc.add_paragraph(f"Карточка в ЕИС: {purchase['url']}")
    facts = [
        ("Кратко", analysis.get("summary", "")),
        ("Срок подачи заявок", purchase.get("deadline", "") or analysis.get("deadline", "")),
        ("Место поставки", analysis.get("delivery_place", "")),
        ("Сроки поставки", analysis.get("delivery_terms", "")),
        ("Обеспечение заявки", analysis.get("bid_security", "")),
        ("Обеспечение контракта", analysis.get("contract_security", "")),
        ("Гарантия", analysis.get("warranty", "")),
    ]
    _table(doc, ["Параметр", "Значение"], [[k, v or "не найдено в документах"] for k, v in facts], [5, 12])
    for title, key in (("Ограничения и запреты", "restrictions"), ("Требования к участнику", "participant_requirements"),
                       ("Документы в составе заявки", "documents_required")):
        doc.add_paragraph()
        doc.add_paragraph().add_run(title).bold = True
        for item in analysis.get(key) or ["не найдено в документах"]:
            doc.add_paragraph(f"☐ {item}")
    doc.add_paragraph()
    doc.add_paragraph().add_run("Подбор оборудования").bold = True
    rows = [[m.get("position", ""), " ".join(x for x in (m.get("brand", ""), m.get("model", "")) if x) or "—",
             m.get("compliance", ""), m.get("notes", "")] for m in matches]
    _table(doc, ["Позиция", "Предложение", "Соответствие", "Что проверить"], rows, [5, 4, 2.5, 5.5])
    _note(doc)
    doc.save(str(path))
    return path
