"""Поиск и аналитика закупок по 44-ФЗ и 223-ФЗ в ЕИС (zakupki.gov.ru).

Модуль разбирает HTML-страницу результатов расширенного поиска ЕИС.
Если сайт изменит вёрстку, правьте функции parse_results / _parse_entry.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup, Tag

from .certs import configure_session
from .relevance import minus_hit, tag_matches

BASE_URL = "https://zakupki.gov.ru"
SEARCH_PATH = "/epz/order/extendedsearch/results.html"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
DELAY_BETWEEN_REQUESTS = 1.5  # секунды, чтобы не перегружать ЕИС


class ProcurementError(Exception):
    """Понятная пользователю ошибка поиска закупок."""


@dataclass
class Purchase:
    number: str
    url: str
    law: str = ""
    method: str = ""
    status: str = ""
    title: str = ""
    customer: str = ""
    price: float | None = None
    currency: str = "₽"
    published: str = ""
    updated: str = ""
    deadline: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchParams:
    query: str
    fz44: bool = True
    fz223: bool = True
    only_active: bool = True
    price_from: float | None = None
    price_to: float | None = None
    page: int = 1
    records_per_page: int = 50


def build_search_url(params: SearchParams) -> str:
    if not (params.fz44 or params.fz223):
        raise ProcurementError("Выберите хотя бы один закон: 44-ФЗ или 223-ФЗ.")
    query: dict[str, Any] = {
        "searchString": params.query,
        "morphology": "on",
        "pageNumber": max(1, int(params.page)),
        "sortDirection": "false",
        "recordsPerPage": f"_{params.records_per_page}",
        "showLotsInfoHidden": "false",
        "sortBy": "UPDATE_DATE",
        "currencyIdGeneral": "-1",
    }
    if params.fz44:
        query["fz44"] = "on"
    if params.fz223:
        query["fz223"] = "on"
    if params.only_active:
        query["af"] = "on"  # этап «Подача заявок»
    else:
        query.update({"af": "on", "ca": "on", "pc": "on", "pa": "on"})
    if params.price_from:
        query["priceFromGeneral"] = int(params.price_from)
    if params.price_to:
        query["priceToGeneral"] = int(params.price_to)
    return f"{BASE_URL}{SEARCH_PATH}?{urlencode(query)}"


_PRICE_CLEAN_RE = re.compile(r"[^\d,.\-]")


def parse_price(text: str) -> float | None:
    """'1 234 567,89 ₽' -> 1234567.89"""
    if not text:
        return None
    cleaned = _PRICE_CLEAN_RE.sub("", text.replace("\xa0", "").replace(" ", ""))
    if not cleaned:
        return None
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_date(text: str) -> date | None:
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text or "")
    if not match:
        return None
    day, month, year = (int(x) for x in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


_INLINE_TAGS = ("span", "b", "strong", "em", "i", "u", "mark", "font")


def _text(node: Tag | None) -> str:
    """Текст узла. Подсветку найденных слов (<span>проектор</span>а) склеиваем без лишних пробелов."""
    if node is None:
        return ""
    if node.find(_INLINE_TAGS):
        node = BeautifulSoup(str(node), "html.parser")
        for tag in node.find_all(_INLINE_TAGS):
            if not tag.find(True):  # только «листовые» строчные теги, вложенную разметку не трогаем
                tag.unwrap()
        for br in node.find_all("br"):
            br.replace_with(" ")
        node.smooth()
    return " ".join(node.get_text(" ", strip=True).split())


def _title_value_pairs(entry: Tag, title_cls: str, value_cls: str) -> dict[str, Tag]:
    """Собирает пары «заголовок -> значение» внутри одной карточки."""
    pairs: dict[str, Tag] = {}
    for title in entry.select(f".{title_cls}"):
        value = title.find_next_sibling(class_=re.compile(value_cls))
        if value is None:
            value = title.find_next(class_=re.compile(value_cls))
        if value is not None:
            pairs.setdefault(_text(title).lower(), value)
    return pairs


def _parse_entry(entry: Tag) -> Purchase | None:
    link = entry.select_one(".registry-entry__header-mid__number a")
    if link is None:
        return None
    number = re.sub(r"[^\d]", "", _text(link))
    if not number:
        return None
    url = urljoin(BASE_URL, link.get("href", ""))

    header_title = entry.select_one(".registry-entry__header-top__title")
    header_parts = [p.strip() for p in header_title.get_text("\n").split("\n") if p.strip()] if header_title else []
    law = header_parts[0] if header_parts else ""
    method = " ".join(header_parts[1:]) if len(header_parts) > 1 else ""

    status = _text(entry.select_one(".registry-entry__header-mid__title"))

    body = _title_value_pairs(entry, "registry-entry__body-title", r"registry-entry__body-(value|href)")
    title = _text(body.get("объект закупки")) or _text(entry.select_one(".registry-entry__body-value"))
    customer = ""
    for key, node in body.items():
        if key.startswith("заказчик") or key.startswith("организация"):
            customer = _text(node)
            break

    price_node = entry.select_one(".price-block__value")
    price_text = _text(price_node)
    price = parse_price(price_text)
    currency = "₽"
    for symbol in ("$", "€", "¥"):
        if symbol in price_text:
            currency = symbol

    dates = _title_value_pairs(entry, "data-block__title", "data-block__value")
    published = updated = deadline = ""
    for key, node in dates.items():
        value = _text(node)
        if key.startswith("размещено"):
            published = value
        elif key.startswith("обновлено"):
            updated = value
        elif "окончание" in key:
            deadline = value

    return Purchase(
        number=number,
        url=url,
        law=law,
        method=method,
        status=status,
        title=title,
        customer=customer,
        price=price,
        currency=currency,
        published=published,
        updated=updated,
        deadline=deadline,
    )


def parse_results(html: str) -> list[Purchase]:
    soup = BeautifulSoup(html, "html.parser")
    purchases: list[Purchase] = []
    for entry in soup.select(".search-registry-entry-block"):
        parsed = _parse_entry(entry)
        if parsed is not None:
            purchases.append(parsed)
    return purchases


class ProcurementClient:
    def __init__(self, ca_bundle: str = "", session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru-RU,ru;q=0.9",
            }
        )
        # Сертификат Минцифры: из папки certs / настроек, иначе — хранилище Windows (см. certs.py).
        self._verify: bool | str = configure_session(self._session, ca_bundle) if session is None else True
        self._last_request = 0.0
        self._lock = threading.Lock()

    def _request(self, url: str) -> requests.Response:
        with self._lock:
            wait = DELAY_BETWEEN_REQUESTS - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                response = self._session.get(url, timeout=REQUEST_TIMEOUT, verify=self._verify)
            except requests.exceptions.SSLError as exc:
                raise ProcurementError(
                    "Не удалось проверить сертификат zakupki.gov.ru. Скачайте сертификаты Минцифры "
                    "«Russian Trusted Root CA» и «Russian Trusted Sub CA» с Госуслуг и положите их в папку "
                    "%APPDATA%\\Jarvis\\certs, затем повторите поиск (см. README, раздел «Проблемы»)."
                ) from exc
            except requests.RequestException as exc:
                raise ProcurementError(f"Сайт ЕИС недоступен: {exc.__class__.__name__}") from exc
            finally:
                self._last_request = time.monotonic()
        if response.status_code == 403:
            raise ProcurementError(
                "ЕИС отклонила запрос (403). Сайт часто блокирует доступ не из России и через VPN."
            )
        if response.status_code != 200:
            raise ProcurementError(f"ЕИС вернула код {response.status_code}.")
        return response

    def _get(self, url: str) -> str:
        response = self._request(url)
        response.encoding = response.encoding or "utf-8"
        return response.text

    def get_text(self, url: str) -> str:
        return self._get(url)

    def get_file(self, url: str) -> tuple[bytes, str]:
        """Скачивает файл. Возвращает (содержимое, имя файла из заголовка Content-Disposition)."""
        response = self._request(url)
        disposition = response.headers.get("Content-Disposition", "")
        name = ""
        match = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", disposition, re.I)
        if match:
            from urllib.parse import unquote

            name = unquote(match.group(1).strip('" '))
        else:
            match = re.search(r'filename="?([^";]+)"?', disposition, re.I)
            if match:
                raw = match.group(1)
                try:  # ЕИС иногда отдаёт имя в cp1251, прочитанное как latin-1
                    name = raw.encode("latin-1").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    try:
                        name = raw.encode("latin-1").decode("cp1251")
                    except (UnicodeEncodeError, UnicodeDecodeError):
                        name = raw
        return response.content, name

    def search(self, params: SearchParams) -> list[Purchase]:
        return parse_results(self._get(build_search_url(params)))

    def search_tags(
        self,
        tags: Iterable[str],
        *,
        fz44: bool = True,
        fz223: bool = True,
        only_active: bool = True,
        price_from: float | None = None,
        price_to: float | None = None,
        pages_per_tag: int = 1,
        strict: bool = False,
        minus_words: list[str] | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> tuple[list[Purchase], list[str]]:
        """Ищет по каждому тегу и объединяет результаты без дублей.

        Возвращает (закупки, список ошибок по тегам).
        """
        merged: dict[str, Purchase] = {}
        errors: list[str] = []
        off_topic = 0
        minus_count: dict[str, int] = {}
        for tag in [t.strip() for t in tags if t and t.strip()]:
            for page in range(1, max(1, pages_per_tag) + 1):
                if on_progress:
                    on_progress(f"Ищу «{tag}», страница {page}…")
                try:
                    found = self.search(
                        SearchParams(
                            query=tag,
                            fz44=fz44,
                            fz223=fz223,
                            only_active=only_active,
                            price_from=price_from,
                            price_to=price_to,
                            page=page,
                        )
                    )
                except ProcurementError as exc:
                    errors.append(f"{tag}: {exc}")
                    break
                for item in found:
                    if strict and not tag_matches(item.title, tag):
                        off_topic += 1
                        continue
                    hit = minus_hit(item.title, minus_words or [])
                    if hit:
                        minus_count[hit] = minus_count.get(hit, 0) + 1
                        continue
                    existing = merged.get(item.number)
                    if existing is None:
                        item.tags = [tag]
                        merged[item.number] = item
                    elif tag not in existing.tags:
                        existing.tags.append(tag)
                if len(found) < 50:
                    break
        items = list(merged.values())
        if off_topic:
            errors.append(f"Скрыто не по теме: {off_topic} (в названии нет слов тега)")
        if minus_count:
            top = ", ".join(f"«{k}» — {v}" for k, v in sorted(minus_count.items(), key=lambda kv: -kv[1])[:5])
            errors.append(f"Скрыто по минус-словам: {sum(minus_count.values())} ({top})")
        if only_active:
            fresh = [p for p in items if is_actual(p)]
            removed = len(items) - len(fresh)
            if removed:
                errors.append(f"Скрыто неактуальных закупок: {removed} (срок подачи прошёл или этап не «Подача заявок»)")
            items = fresh
        return items, errors


def is_actual(purchase: Purchase, today: date | None = None) -> bool:
    """Закупка ещё принимает заявки: этап «Подача заявок» и срок не истёк.

    ЕИС иногда оставляет старые закупки в этапе «Подача заявок», хотя срок давно прошёл.
    """
    today = today or date.today()
    status = purchase.status.lower()
    if status and "подач" not in status:
        return False
    deadline = parse_date(purchase.deadline)
    if deadline is not None:
        return deadline >= today
    # Срок не указан: считаем актуальной, только если закупку обновляли за последние 60 дней.
    touched = parse_date(purchase.updated) or parse_date(purchase.published)
    return touched is not None and (today - touched).days <= 60


def analyze(purchases: list[Purchase], today: date | None = None) -> dict[str, Any]:
    """Сводная аналитика по найденным закупкам."""
    today = today or date.today()
    with_price = [p for p in purchases if p.price is not None and p.currency == "₽"]
    total = sum(p.price for p in with_price)  # type: ignore[misc]

    by_law: Counter[str] = Counter()
    for p in purchases:
        if "44" in p.law:
            by_law["44-ФЗ"] += 1
        elif "223" in p.law:
            by_law["223-ФЗ"] += 1
        else:
            by_law[p.law or "другое"] += 1

    by_tag: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "sum": 0.0})
    for p in purchases:
        for tag in p.tags or ["без тега"]:
            by_tag[tag]["count"] += 1
            if p.price is not None and p.currency == "₽":
                by_tag[tag]["sum"] += p.price

    customers: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "sum": 0.0})
    for p in purchases:
        if p.customer:
            customers[p.customer]["count"] += 1
            if p.price is not None and p.currency == "₽":
                customers[p.customer]["sum"] += p.price
    top_customers = sorted(customers.items(), key=lambda kv: (-kv[1]["sum"], -kv[1]["count"]))[:10]

    soon: list[dict[str, Any]] = []
    for p in purchases:
        deadline = parse_date(p.deadline)
        if deadline is not None and 0 <= (deadline - today).days <= 3:
            soon.append({"number": p.number, "title": p.title, "deadline": p.deadline, "days_left": (deadline - today).days})
    soon.sort(key=lambda x: x["days_left"])

    largest = sorted(with_price, key=lambda p: p.price or 0, reverse=True)[:5]

    return {
        "count": len(purchases),
        "total_sum": round(total, 2),
        "average_price": round(total / len(with_price), 2) if with_price else 0,
        "by_law": dict(by_law),
        "by_tag": {k: {"count": int(v["count"]), "sum": round(v["sum"], 2)} for k, v in by_tag.items()},
        "top_customers": [
            {"customer": name, "count": int(v["count"]), "sum": round(v["sum"], 2)} for name, v in top_customers
        ],
        "deadline_soon": soon[:10],
        "largest": [p.to_dict() for p in largest],
    }


def export_to_excel(purchases: list[Purchase], path: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Закупки"
    headers = [
        "Номер", "Закон", "Способ", "Этап", "Объект закупки", "Заказчик",
        "НМЦК", "Валюта", "Размещено", "Обновлено", "Окончание подачи", "Теги", "Ссылка",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for p in purchases:
        ws.append([
            p.number, p.law, p.method, p.status, p.title, p.customer, p.price, p.currency,
            p.published, p.updated, p.deadline, ", ".join(p.tags), p.url,
        ])
        link_cell = ws.cell(row=ws.max_row, column=len(headers))
        link_cell.hyperlink = p.url
        link_cell.style = "Hyperlink"
    widths = [22, 10, 24, 16, 60, 45, 16, 8, 12, 12, 16, 25, 40]
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width
    for row in ws.iter_rows(min_row=2, min_col=7, max_col=7):
        for cell in row:
            cell.number_format = "#,##0.00"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    summary = analyze(purchases)
    ws2 = wb.create_sheet("Сводка")
    ws2.append(["Всего закупок", summary["count"]])
    ws2.append(["Сумма НМЦК, ₽", summary["total_sum"]])
    ws2.append(["Средняя НМЦК, ₽", summary["average_price"]])
    ws2.append([])
    ws2.append(["Тег", "Количество", "Сумма, ₽"])
    for tag, v in summary["by_tag"].items():
        ws2.append([tag, v["count"], v["sum"]])
    ws2.append([])
    ws2.append(["Заказчик", "Количество", "Сумма, ₽"])
    for c in summary["top_customers"]:
        ws2.append([c["customer"], c["count"], c["sum"]])
    ws2.column_dimensions["A"].width = 60
    ws2.column_dimensions["B"].width = 14
    ws2.column_dimensions["C"].width = 18

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


class SeenStore:
    """Помнит номера уже показанных закупок, чтобы мониторинг сообщал только о новых."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        try:
            self._seen: set[str] = set(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError):
            self._seen = set()

    def filter_new(self, purchases: list[Purchase]) -> list[Purchase]:
        with self._lock:
            new = [p for p in purchases if p.number not in self._seen]
            self._seen.update(p.number for p in purchases)
            if len(self._seen) > 20000:
                self._seen = set(list(self._seen)[-10000:])
            self._path.write_text(json.dumps(sorted(self._seen)), encoding="utf-8")
        return new

    def is_empty(self) -> bool:
        with self._lock:
            return not self._seen


def format_money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def summary_text(purchases: list[Purchase], limit: int = 15) -> str:
    """Короткий текстовый отчёт — для ответа ИИ и голосового помощника."""
    stats = analyze(purchases)
    lines = [
        f"Найдено закупок: {stats['count']}, общая НМЦК: {format_money(stats['total_sum'])}.",
        "По законам: " + ", ".join(f"{k} — {v}" for k, v in stats["by_law"].items()) if stats["by_law"] else "",
    ]
    for p in sorted(purchases, key=lambda x: x.price or 0, reverse=True)[:limit]:
        lines.append(
            f"- №{p.number} | {p.law} | {p.title[:150]} | {p.customer[:80]} | "
            f"{format_money(p.price)} | до {p.deadline or '—'} | {p.url}"
        )
    return "\n".join(line for line in lines if line)


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M")
