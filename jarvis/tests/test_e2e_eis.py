"""Офлайн-проверка всей цепочки ЕИС → документы → разбор ТЗ → стоимость → аналитика.

zakupki.gov.ru недоступна из этой среды, поэтому подменяется requests-сессия: она отдаёт HTML, повторяющий
вёрстку настоящей ЕИС (карточки поиска, карточка 44-ФЗ zk20, страница документов с дублями-подписями, старая
карточка 223-ФЗ с неэкранированным «&noticeGuid»), и настоящие .docx/.xlsx/.pdf/.doc (Word 97)/.rtf/.zip.
ИИ — это настоящий LLM(provider=gigachat), у которого подменён только HTTP-ответ GigaChat: JSON в тексте и
в ```-блоках, висящие запятые, числа строками, голый список вместо объекта, оборванный по лимиту первый ответ.
"""

from __future__ import annotations

import base64
import io
import json
import re
import zipfile
import zlib
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from jarvis_app import analyst as an
from jarvis_app import cloud_llm, procurement
from jarvis_app.config import Config
from jarvis_app.procurement import ProcurementClient
from jarvis_app.relevance import DEFAULT_MINUS_WORDS
from jarvis_app.tender import TenderWorkspace

WORD97_DOC_B64 = (
    b"eNrtXG9sW9UVP/f52n7un9QJIS2lECeYtJQ2OI6dOH/rOG0IaZqkf9R2jJamjUsKTW3SoLV8KoxJFdKkMiatHyYGE5OQQFvpJjFp"
    b"sBWGKr4gijYq+FamfZm0D221aUITzc459z37vednxw5/O3yjm/fue+f8zrn3nHvuH1/70ge1V156ffWn4Eg94IEb8wHwWZ4JzHeZ"
    b"hSCAZjy7MT8/bz6er6abKn1uXMmGzWg/L2ayuR+zjjmAeQnmpZiXYV6OuQbzCuUCUIu5DvMtmOsx34q5AfNKzKsM31iN19sxr8F8"
    b"B+Y7MTdiDmFuMmgoh437avr60g7I4N8c2mILHMPrLJyESlIDeowVrxyeG2XSlZuq8hcvX1j6n3T0f3pm9v9qv/z/TAIt61mifMgX"
    b"0NgnLijXGKL4ve3IodnM8czhudCezOzUxs2Zx56YSR+bY5/YtpOebc4c4pGA7luxwO9bE/CvrvOPL+yLQk0jFp1uzBOG5oYLV370"
    b"i+ufjU8HX31Oh3vX/vaTCD0zxjR6P4GZfH6v4fNHDb8/Y9C8bPj+OWP8e9MYA98zxsG/glL+H5+rMY3eZU/OTWeObZzKHDrBz27D"
    b"/Ej6WHp2ci49FTp4MmQlgDzfWGZ2ZvJo61RmboafEW4b5qSlTgX3s9vfj8EHwnpfafvVwXaso4Dv84iu0iwO7dfmNbpiu4zh+DAL"
    b"MzDJ7UPjdt1UvRDgCcNkGNrC4hZsp/qX34OBMKwPC2iH1HoYH5GwHfPgSAPMDAfkccxbh7HJR7xwDO/nMO8f8cEkvn94WEg4ACJI"
    b"djzAbT8MaZQ3BUdwTHoEx6Y2nmEI8IZFsgVuefmKqEehPpS2FqUFstMdDz+HMv0o048y10BcFxMjOigRa+AhXSCyVkJClCVoSgJW"
    b"6T1wSBh/6i2LhNV2CatZwl7wsIS9rhLaWYJHSdCsEhoLJVjQCXk/SEbe74ocY2SpkD1W5LsLkTt0YQHfpwvC3wFext/hih9nfK/C"
    b"l1b82xR+eNOBHL6JjK3gK9EaHYzpU5hel9awYnbYFEZEfwnkTkb2K2SfE1lkp5PJZDHkB0Fn5AddkROMrCtkvxW5yc1H6iHvIDg1"
    b"PgCBEv7XxdgBha07/c+hNWF3WP2vnrXPpxa40VgnruGVIpcpK439OASDMI2lWQudEIpuyJgLFtL1It31+VrRyzO+zUhxGN89gRGB"
    b"Zo4TTDeJ9aD/WeQLMRbNJyntYilNYhevHQr7tlVW+f1ZobYVQY26oZbRh0cZdUCMuqK221AX7rdjjLZdjPFIsgtx5rDN0jaUexTK"
    b"yo59Ft1ikDztha3DmsWHsJvvZrxJsZtHoJ1ogYOI6IZ6r1uNVyHqCrvnrGLP2cC4j4sNXOsU2m4K1wEh1DgNJ9jGVm+IMPVTIsJr"
    b"QDfqqIMjwRzPiURRDnvL6qhsELULQj9zviT6ec25Dd8fQopMEd2CWNkAVjbAXYT6RYz5XxcxtsB2bDHl4VauFaqHkaLYNvs4HhLX"
    b"22KHqxfEbNzlxtghxvxQDLlixm2YpeKq8tBPi3hoh5uHloilCu1aEbTOArTS8VP5u9DGXNESNrRyYqbq47Waex/vKujj5cXJZtgT"
    b"xHikNfP8ZievejOMSeU4m6sF9iJNm9bC3rgF5z9ZlnQcpR9nOuUqIypeaCM8l3qAY14a/x9nH3P3t1vzNVeeYniI8vXtmvJ1s3er"
    b"nl2oAXntiuz0qVOnTK9VHjapDXFPcOpSiFBMj6SKBlqScex67ODYTyMEIR/C/6RJty5S633Z6QsT26B/velVT2mjvEPk1MQN4zYb"
    b"BgU/xFGmwGjC0UOLch9O8Sj1GEcAM/JRNER+YjN4WnjEeklTI9so13gOr5NYlzTyqDklGHSvl6CLWujeLkHXbqH7sARdzEL3aQm6"
    b"uIXuWgm6Dgud8BSn67TQ1ZagS1jomkrQdalFItIJSHmGeO1k76FklXacrV/FucwJnsuswq6JURgzDeoTI0uxXy7jEb0VUkHCaeXZ"
    b"UeEIQZaPgVpqnOAx6D6k3+GJ8BrO1I98Og4Hnjl7Y/8z07mFyUxY8IqpmedfSdZ0kGcqNHZSz6c+3oHrmqu4xliZ7xWDI3XkT5N4"
    b"MX1KwCy3iZe9eoq1o1InLEMpEuuzHJGI7mlPB3u/ffY1yVwh21yLVnvU6t3QiBr+xNPN8cXKR/unXYZ+yy/qoA3XnQkLTVWQ7dAN"
    b"IeT9pcFrR78d35bifRL1vTZ/3vMkt/IYj647sXVorDUt2Qg9BoZj3QdfbN33AvSzVV7gVrT26D0461QSlaANAu0YniKFxy7q5kz3"
    b"bn4wnn/QMqWLgJy4qJMBdZSwlim25ykqmnFNwAjrN8E+4pxxkeX6KpxrJdk7/uZJ8phmeq7bLJq8px/2P3NJs3tyghGuexLcN93n"
    b"XxS5N2H7XRHC0mu6mVOT3UU529kHkjme/Ewsxrx1MsYeltc7yr1uwOh1Tl0VV3MBV7safwyuhLRz9UIEWz0qe23RJ4T0tOaglp/j"
    b"zxsGYUkLLB0yq2iwJ6GDYolMcgRy51Z61+NahhE0J0InRZcFENoZYYtC8NgReqENEQ4W1GAMEWY4hs4y95Dilk753cg9WyDfym3W"
    b"4H6F4HUi9CDC0wsgqBoMKwSfHaEfNlM0kv0OhEFjZUeR4QnuAzTC5+PtCcOGKdhCEUmmeIZQnF/VoxZGLJ6exxhCjPNlYLQzxlbI"
    b"+5KJ8SxHt3fksxxhZnIrCMCIOurcy1p+cSX4k9opecEHgeTSU8svmNtiBbtczpUGRr9SEY+jXavqf9DKuuRnhxRHtrmsQ84a9GfZ"
    b"BsVnl7QyHMO6v+LZh1ntHqwqK1jKfLCUbsGy2ByxFZqCbZ7rsjWnmRoLQwVzAA3G8X8rtAZrjZovxXgzjjYspPViFEy2BAzZBwXv"
    b"zwbNzzjoA8l3l6i9WvPez6O5MHZ051c4/9ROqlbknQdUdnsnjd1ht3de45MSt3c+YzfZ7Z3f2GF2eyeMT02dScf6U++iSHnrRS+I"
    b"aeEbH6lB36tBnjeuitx+tyMxX0Lm+BLSpwFZrjTHi94cx4te5NAW5ND9OQ7djxyeBTmyeo4jqyOHXJDj40CO4+MAcngX5EguzXEk"
    b"lyKHb0GOV5blOF5Zhhz+BTmCNTmOYA1y6AZHcTte0nIcl7SqHW9aO9rtUbXjt9uOxTnscVXjkapqwZvJgvaIWrXgzWdBpz2qFvx2"
    b"WtByuEizcwCu43GtQBw8+a1Sf73UfhdrOs/EeBxl6Sh7HWWfo+wvceqsMPkZYQ/+qR2HNlspaiu120pxW6nDVuq0rAy5jkHLgwl1"
    b"YpUPstzfcKasozC04TiDi9DjvDWShh/wxxMZ3iagbeE44pRzPmknnORtlQwfkmlvKU/6AMyifHWw5ghpXLswD22CqO2bydz2NW3X"
    b"phnrcBl16iyzbWiLnOqkdMRa+crj2zjfPh+CmsN34Ir+njJ5KtPqKLcB6YV8FWn1u8gbkT9HfhMBGCd5ejnyMuh6JEtt2dnLZguj"
    b"B5DNg4u33yRv69h9YmuZddvDm0K0HUQo30P+EDzIz9QWC7VTV5lYQ/yxXNqiUVf4jBCL4ozLxfSeFIYmof94FcAlZJ5m0R+8tWYt"
    b"5fywRR/f7RVU0jmY/fCMXvDUNO9aD581L4x51VTZYcHIl4/p8I9QmWwVa3Kqaj639M9fCbEtFMCOcbXuT9amNfZXa2BwMnN08ljC"
    b"pcEDsgH6LCHvoFjonPoKnlUI495vuXem0/z/bD3YrqUS0dRXUPPX6tXJ298bVzP9pRnggueLt+z55sXxPd+CLWnpFX9sVt+qqabF"
    b"pXfRvhfxerFJnez+ptNutOdDDj9Z9g3oocEnVeeopmr6TqdGmZQJ2SP7ZYeMyz68j8sQNDSpU67deJ+QnTKKTx8w3o/J5dCMXHHZ"
    b"hlxRGeGn41JRD+CzGL5LIl7EoN8A6vmATOGzqBxEuk68G8f7bqQIQR+WQjAo78crUSktogZdN3Jswv9xlNwGrYi2MsdNfDHE7kF5"
    b"CsWuQUqOMkUcaXuwnMQ8xFqFoJ+pU/ysjyVuwvt+fDsgt3CLxIw6U+1SRm2TLGUdvAohrtMAv/8DnZxDCQlusT7Wz4oekfeg5ssh"
    b"yvrflaspoXc7NExgHsRyytBI4cXwbZxrqVpy1GjxJrlGNuK1PkQoPSy3X7YbchMoKV8HawuPM7rZBq18hiLC5xNjsBHCcqUkfeuR"
    b"gtq/l1FS0g93ot3HZSe3bRzljVo08uOcrk5uRWtRGyrrkP+EgPwlLs32oLLZdl1M2Y9Xs/17EWGYa9vH2Pk3MdSkm+2XQFkxRPIX"
    b"7EtVU0XJmOdX03cy8T5JB+ZzmLMBXH+jL0QxT2DegflNzO8sUT7yuZXzv9bCZ9bCf6yFvxctnF4E2VefcGbeEBYrw+KBw7Q01S9H"
    b"QPvp/FxwZ1O5CIHLvMtUd5kWo/NgbuMbd9mKHscu+0AIj3GjeY6qG2k+0fHJ4Rh4YvT7Cw2m0sW/n5k7KGXmJT3QG4HkBEBqQoPG"
    b"c5fuC5270td0Tvc3Y77rzIveMOYovm87My0iWdrhbMfSuijURKrd5+ZPxb4/zafmPnr/o5+33h58/mc63Lvhs19vxmdexzNaya6y"
    b"OFQQ8t+JpjPTHsu+njq7rN4njTLd03erB7LZ3enZ40cyx+zfqeZN3VhrBFPVWF9B+jJ//4Ps6fwNATce8okLmjkADfJHU1kYh4Pw"
    b"aMX614HG/sInNSv4XY1HcwPguHGSf3FpCUonuZ4K5JO+5k5lG+zi79QsVocaQ34lv/9BbfXvderey99pmOEPB8n2dJL3cO4b//nv"
    b"xxRL6+g782CceC1TPrVV0Ngr3sOypmAzXg+xJunc94bLSasXUX/6TQZz39pbILmy9kgsQj7FtNe+xD78RX7/5X+wVkfW")

N44 = "0318100005226000001"
N223 = "32616576327"
N_OTHER = "0373200000126000123"
GUID = "38f2fc7f-c0f9-48d6-b181-e4386a620809"
HOST = "https://zakupki.gov.ru"
CARD44 = f"{HOST}/epz/order/notice/zk20/view/common-info.html?regNumber={N44}"
DOCS44 = f"{HOST}/epz/order/notice/zk20/view/documents.html?regNumber={N44}"
CARD223 = f"{HOST}/223/purchase/public/purchase/info/common-info.html?regNumber={N223}"
DOCS223 = f"{HOST}/epz/order/notice/notice223/documents.html?purchaseNoticeNumber={N223}&noticeGuid={GUID}"


# ---------- файлы ----------
def _docx(paragraphs: list[str], rows: list[tuple[str, str]] = ()) -> bytes:
    import docx

    d = docx.Document()
    for text in paragraphs:
        d.add_paragraph(text)
    if rows:
        table = d.add_table(rows=len(rows), cols=2)
        for i, (a, b) in enumerate(rows):
            table.cell(i, 0).text, table.cell(i, 1).text = a, b
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _xlsx(rows: list[list]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pdf(lines: list[str]) -> bytes:
    """Минимальный PDF со шрифтом Helvetica (ASCII-текст); без строк — «скан» без текста."""
    content = ("BT /F1 12 Tf 72 720 Td " + " ".join(f"({x}) Tj 0 -16 Td" for x in lines) + " ET") if lines else ""
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def _zip_cp866(members: dict[str, bytes]) -> bytes:
    """Архив как из Windows-архиватора: имена в cp866 без флага UTF-8."""
    buf = io.BytesIO()
    original = zipfile.ZipInfo._encodeFilenameFlags
    zipfile.ZipInfo._encodeFilenameFlags = lambda self: (self.filename.encode("cp437"), self.flag_bits & ~0x800)
    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in members.items():
                zf.writestr(name.encode("cp866").decode("cp437"), data)
    finally:
        zipfile.ZipInfo._encodeFilenameFlags = original
    return buf.getvalue()


TZ_ROWS = [("Наименование характеристики", "Требование заказчика"), ("Световой поток", "не менее 6000 лм"),
           ("Разрешение", "не менее WUXGA 1920x1200"), ("Источник света", "лазерный, ресурс не менее 20 000 ч")]
TZ_ROWS += [(f"Параметр {k}", "значение в соответствии с требованиями заказчика, подтверждается документацией производителя " * 2)
            for k in range(60)]
TZ = _docx(["Описание объекта закупки", "Поставка лазерного проектора — 1 шт. Место поставки: г. Красноярск.",
            "Срок поставки: 30 календарных дней с даты заключения контракта. Требуется монтаж и настройка."], TZ_ROWS)
CONTRACT = _docx(["Типовой контракт поставки"] + ["Общие положения, права и обязанности сторон." * 3] * 80
                 + ["Оплата в течение 7 рабочих дней с даты подписания документа о приёмке.", "Гарантийный срок — 36 месяцев."])
NMCK_XLSX = _xlsx([["Обоснование НМЦК"], ["Позиция", "Цена 1", "Цена 2", "Цена 3", "НМЦК"],
                   ["Лазерный проектор", 215000, 220000, 221000, 218667]])
NMCK_PDF = _pdf(["NMCK obosnovanie", "Lazernyi proektor 218667 RUB"])
SCAN_PDF = _pdf([])
DECL = _docx(["Декларация о стране происхождения товара: Российская Федерация."])
NOTICE_RTF = (b"{\\rtf1\\ansi\\ansicpg1251{\\fonttbl{\\f0 Times New Roman;}}{\\*\\generator Word;}\\f0 "
              + "".join(f"\\'{b:02x}" if b > 127 else chr(b) for b in "Извещение о запросе котировок".encode("cp1251")).encode()
              + b"\\par \\u1053?\\u1052?\\u1062?\\u1050? 218 667,00\\par}")
ZIP = _zip_cp866({"Обоснование цены контракта.docx": _docx(["Обоснование цены: метод сопоставимых рыночных цен."]),
                  "Обоснование цены контракта.docx.sig": b"0\x82\x05\x00signature"})
BLOCKED = "<!DOCTYPE html><html><head><title>Доступ ограничен</title></head><body>Доступ ограничен</body></html>".encode()
TZ223 = _docx(["Техническое задание на поставку проектора для актового зала. Количество: 2 шт."],
              [("Световой поток", "не менее 5000 лм"), ("Контрастность", "не менее 100000:1")])
CONTRACT223 = _docx(["Проект договора. Срок поставки — 20 рабочих дней. Оплата — 10 рабочих дней."])
NOTICE223 = _docx(["Извещение о проведении запроса предложений в электронной форме № 32616576327."])
PRICE223 = _xlsx([["Расчёт цены договора"], ["Проектор", 2, 1066758]])


# ---------- страницы ЕИС ----------
def _d(days: int) -> str:
    return (date.today() + timedelta(days=days)).strftime("%d.%m.%Y")


def _card(number, href, law, method, status, title_html, customer, price, deadline_days, updated_days=-1):
    return f"""
<div class="search-registry-entry-block box-shadow-search-input">
 <div class="row no-gutters registry-entry__form mr-0">
  <div class="registry-entry__header">
   <div class="registry-entry__header-top__title text-truncate">
     {law}
     <br>
     {method}
   </div>
   <div class="registry-entry__header-mid__number"><a href="{href}" target="_blank">№ {number}</a></div>
   <div class="registry-entry__header-mid__title text-normal">{status}</div>
  </div>
  <div class="registry-entry__body">
   <div class="registry-entry__body-block">
    <div class="registry-entry__body-title">Объект закупки</div>
    <div class="registry-entry__body-value">{title_html}</div>
   </div>
   <div class="registry-entry__body-block">
    <div class="registry-entry__body-title">Заказчик</div>
    <div class="registry-entry__body-href"><a href="/epz/organization/view/info.html?organizationCode=1">{customer}</a></div>
   </div>
  </div>
  <div class="price-block"><div class="price-block__title">Начальная цена</div>
   <div class="price-block__value">{price}&nbsp;₽</div></div>
  <div class="data-block mt-auto">
   <div class="data-block__title">Размещено</div><div class="data-block__value">{_d(-5)}</div>
   <div class="data-block__title">Обновлено</div><div class="data-block__value">{_d(updated_days)}</div>
   <div class="data-block__title">Окончание подачи заявок</div><div class="data-block__value">{_d(deadline_days)} 10:00 (МСК+4)</div>
  </div>
 </div>
</div>"""


SEARCH = "<html><body><div class='search-results'>" + "".join([
    _card(N44, f"/epz/order/notice/zk20/view/common-info.html?regNumber={N44}", "44-ФЗ",
          "Запрос котировок в электронной форме", "Подача заявок", 'Лазерный <span class="highlight">проектор</span>',
          "ФГБНУ «КРАСНОЯРСКИЙ НАУЧНЫЙ ЦЕНТР»", "218 667,00", 6),
    _card(N223, f"/223/purchase/public/purchase/info/common-info.html?regNumber={N223}", "223-ФЗ",
          "Запрос предложений в электронной форме", "Подача заявок", "Поставка <b>проектор</b>а",
          "МАУК «ДОМ КУЛЬТУРЫ»", "2 133 516,00", 9),
    _card(N_OTHER, f"/epz/order/notice/ea20/view/common-info.html?regNumber={N_OTHER}", "44-ФЗ", "Электронный аукцион",
          "Подача заявок", "Поставка мультимедийного <span>проектор</span>а для школы", "МБОУ СОШ № 5", "350 000,00", 4),
    _card("0373200000126000999", "/epz/order/notice/ea20/view/common-info.html?regNumber=0373200000126000999", "44-ФЗ",
          "Электронный аукцион", "Подача заявок", "Ремонт кровли здания", "Администрация", "1 000 000,00", 5),
    _card("0373200000126000998", "/epz/order/notice/ea20/view/common-info.html?regNumber=0373200000126000998", "44-ФЗ",
          "Электронный аукцион", "Подача заявок", "Поставка медицинского оборудования с <b>проектор</b>ом",
          "Городская больница", "5 000 000,00", 5),
    _card("0373200000126000997", "/epz/order/notice/ea20/view/common-info.html?regNumber=0373200000126000997", "44-ФЗ",
          "Электронный аукцион", "Подача заявок", "<span>Проектор</span> для актового зала", "Школа № 1", "90 000,00", -20, -40),
    _card("0373200000126000996", "/epz/order/notice/ea20/view/common-info.html?regNumber=0373200000126000996", "44-ФЗ",
          "Электронный аукцион", "Работа комиссии", "<span>Проектор</span> интерактивный", "Школа № 2", "120 000,00", 3),
]) + "</div></body></html>"

CARD44_HTML = f"""<html><body><div class="tabsNav">
<a class="tabsNav__item tabsNav__item_active" href="/epz/order/notice/zk20/view/common-info.html?regNumber={N44}">Общая информация</a>
<a class="tabsNav__item" href="/epz/order/notice/zk20/view/documents.html?regNumber={N44}">Документы</a>
<a class="tabsNav__item" href="/epz/order/notice/zk20/view/event-journal.html?regNumber={N44}">Журнал событий</a>
</div><span class="cardMainInfo__purchaseLink">Лазерный проектор</span></body></html>"""

FILES44 = [  # (uid, title на странице, байты, Content-Disposition)
    ("A1", "Приложение № 1 Описание объекта закупки_лазерный проектор ФГБНУ КНЦЗВ итог.docx", TZ, None),
    ("A2", "Приложение № 2 Типовой контракт поставки лазерного проектора.docx", CONTRACT, None),
    ("A3", "Приложение № 3 НМЦК.xlsx", NMCK_XLSX, None),
    ("A4", "Приложение № 3 НМЦК.pdf", NMCK_PDF, None),
    ("A5", "Приложение № 4 Требования к заявке.doc", None, None),  # Word 97 из LibreOffice, см. WORD97_DOC_B64
    ("A6", "Приложение № 5 Форма заявки.docx", BLOCKED, None),  # вместо файла — «Доступ ограничен» с кодом 200
    ("A7", "Декларация о стране происхождения.docx", DECL, None),
    ("A8", "Уведомление Минпромторг.pdf", SCAN_PDF, None),
    ("A9", "Извещение о проведении запроса котировок", NOTICE_RTF,
     "attachment; filename*=UTF-8''" + quote("Извещение о проведении запроса котировок.rtf")),
    ("B1", "Документация.zip", ZIP, None),
]


def _docs44_html() -> str:
    blocks = []
    for uid, title, _, _ in FILES44:
        blocks.append(f"""
<div class="attachment row">
 <div class="attachment__value">
  <a href="{HOST}/epz/order/notice/signview/ep/listModal.html?reestrNumber={N44}&uid={uid}" title="Просмотр подписи">
   <img src="/epz/static/img/icons/icon_signature.svg"></a>
  <span class="section__value"><a href="{HOST}/44fz/filestore/public/1.0/download/priz/file.html?uid={uid}" title="{title}">
   <img src="/epz/static/img/icons/icon_doc.svg"> {title} </a> <span class="text-muted">(32,1 Кб)</span></span>
 </div></div>""")
    # прежняя редакция того же ТЗ: другая ссылка, те же байты — не должна попасть в разбор дважды
    blocks.append(f"""<div class="attachment row notice-documents__previous">
  <a href="{HOST}/44fz/filestore/public/1.0/download/priz/file.html?uid=OLD1"
     title="Приложение № 1 Описание объекта закупки_лазерный проектор ФГБНУ КНЦЗВ итог.docx">ТЗ (прежняя редакция)</a></div>""")
    return "<html><body><div class='blockFilesTabDocs'>" + "".join(blocks) + "</div></body></html>"


CARD223_HTML = f"""<html><body><table><tr><td>
<a href="/epz/order/notice/notice223/common-info.html?purchaseNoticeNumber={N223}&noticeGuid={GUID}">Общие сведения</a>
<a href="/epz/order/notice/notice223/documents.html?purchaseNoticeNumber={N223}&noticeGuid={GUID}">Документы</a>
</td></tr></table></body></html>"""

DOCS223_HTML = """<html><body><table class="blockInfo__table">
<tr><td>Документация</td><td><a href="/223/purchase/public/download/download.html?id=101" title="">Техническое задание на поставку проектора.docx (45,1 Кб)</a></td></tr>
<tr><td>Проект договора.docx</td><td><a href="/223/purchase/public/download/download.html?id=102">Скачать</a></td></tr>
<tr><td><a href="/223/purchase/public/download/download.html?id=103">Извещение</a></td></tr>
<tr><td><a href="/223/purchase/public/download/download.html?id=104" title="Приложение 1 Расчёт цены">Приложение 1 Расчёт цены</a></td></tr>
</table></body></html>"""

FILES223 = {
    "101": (TZ223, None),
    "102": (CONTRACT223, 'attachment; filename="' + "Проект договора.docx".encode("utf-8").decode("latin-1") + '"'),
    "103": (NOTICE223, 'attachment; filename="' + "Извещение о закупке.docx".encode("cp1251").decode("latin-1") + '"'),
    "104": (PRICE223, None),  # ни расширения, ни Content-Disposition — формат определяется по содержимому
}


class FakeEIS:
    """requests.Session, отвечающая как zakupki.gov.ru."""

    def __init__(self) -> None:
        self.headers = CaseInsensitiveDict()
        self.asked: list[str] = []

    def mount(self, *args, **kwargs) -> None:
        pass

    @staticmethod
    def _resp(url: str, body: bytes, headers: dict[str, str], status: int = 200) -> requests.Response:
        r = requests.Response()
        r.status_code, r._content, r.url = status, body, url
        r.headers = CaseInsensitiveDict(headers)
        r.encoding = requests.utils.get_encoding_from_headers(r.headers)
        return r

    def get(self, url: str, timeout=None, verify=None) -> requests.Response:
        self.asked.append(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        html = {"Content-Type": "text/html;charset=UTF-8"}
        if parsed.path == "/epz/order/extendedsearch/results.html":
            # без charset в заголовке: requests прочитал бы кириллицу как latin-1
            return self._resp(url, SEARCH.encode("utf-8"), {"Content-Type": "text/html"})
        if url == CARD44:
            return self._resp(url, CARD44_HTML.encode(), html)
        if url == DOCS44:
            return self._resp(url, _docs44_html().encode(), html)
        if url == CARD223:
            return self._resp(url, CARD223_HTML.encode(), html)
        if url == DOCS223:
            return self._resp(url, DOCS223_HTML.encode(), html)
        if parsed.path == "/44fz/filestore/public/1.0/download/priz/file.html":
            uid = query["uid"][0]
            for item_uid, title, body, disposition in FILES44 + [("OLD1", "", TZ, None)]:
                if item_uid == uid:
                    if body is None:
                        body = zlib.decompress(base64.b64decode(WORD97_DOC_B64))
                    if body is BLOCKED:
                        return self._resp(url, body, html)
                    headers = {"Content-Type": "application/octet-stream"}
                    if disposition:
                        headers["Content-Disposition"] = disposition
                    return self._resp(url, body, headers)
        if parsed.path == "/223/purchase/public/download/download.html" and query.get("id", [""])[0] in FILES223:
            body, disposition = FILES223[query["id"][0]]
            headers = {"Content-Type": "application/octet-stream"}
            if disposition:
                headers["Content-Disposition"] = disposition
            return self._resp(url, body, headers)
        return self._resp(url, b"<html><body>Not found</body></html>", html, status=404)


# ---------- «GigaChat» с грязными ответами ----------
ANALYSIS_1 = {
    "summary": "Поставка лазерного проектора", "object": "Поставка «Лазерного проектора»", "delivery_place": "г. Красноярск",
    "delivery_terms": "30 календарных дней с даты заключения контракта", "deadline": "", "contract_term": "",
    "payment_terms": "7 рабочих дней", "acceptance_terms": "", "installation": "монтаж и настройка", "penalties": ["штраф", "пени"],
    "bid_security": None, "contract_security": 5, "warranty": "36 мес.", "participant_requirements": "СМП",
    "documents_required": ["Декларация о стране происхождения"], "restrictions": "ПП РФ № 1875",
    "positions": [{"name": "Лазерный проектор", "quantity": 1, "unit": "шт", "okpd2": 26.2,
                   "characteristics": [{"name": "Световой поток", "requirement": "не менее 6000 лм"},
                                       {"name": "Разрешение", "requirement": "не менее WUXGA"}]}],
}
ANALYSIS_2 = {"positions": [{"name": "лазерный  проектор ", "quantity": "", "characteristics": [
    {"name": "световой поток", "requirement": "дубль"}, {"name": "Срок службы источника", "requirement": 20000}]}],
    "restrictions": ["пп рф № 1875", "Нацрежим"]}
ANALYSIS_223 = {"summary": "Поставка проектора", "positions": [{"name": "Проектор", "quantity": "2", "unit": "шт",
                "characteristics": [{"name": "Световой поток", "requirement": "не менее 5000 лм"}]}]}


def _dirty(data: dict) -> str:
    """JSON «как у GigaChat»: пояснение, ```-блок, висящие запятые."""
    text = json.dumps(data, ensure_ascii=False, indent=1)
    text = re.sub(r"\n(\s*)([}\]])", r",\n\1\2", text)  # висящая запятая перед каждой закрывающей скобкой
    return f"Конечно! Вот результат разбора {{по схеме}}:\n```json\n{text}\n```\nЕсли нужно — уточню."


class FakeGigaChat:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.analysis_calls = 0

    def post(self, body: dict) -> dict:
        system = body["messages"][0]["content"]
        last = body["messages"][-1]["content"]
        assert body["max_tokens"] == 8000
        if system.startswith("Ты — специалист по госзакупкам"):
            self.calls.append("analysis")
            if "Ошибка: ответ оборвался" in last:
                return {"content": _dirty(ANALYSIS_1), "finish_reason": "stop"}
            self.analysis_calls += 1
            if "проектора для актового зала" in last:
                return {"content": _dirty(ANALYSIS_223), "finish_reason": "stop"}
            if self.analysis_calls == 1:  # первый ответ оборвался на лимите токенов
                full = json.dumps(ANALYSIS_1, ensure_ascii=False)
                return {"content": full[: int(len(full) * 0.6)], "finish_reason": "length"}
            data = ANALYSIS_1 if "(1/" in last else ANALYSIS_2
            return {"content": _dirty(data), "finish_reason": "stop"}
        if system.startswith("Ты подбираешь"):
            self.calls.append("match")
            return {"content": 'Подобрал:\n[{"position_index": "0", "catalog_id": "", "brand": "Epson", "model": "EB-L630U", '
                               '"country": "Филиппины", "compliance": "Соответствует", "price": "", "offered_values": '
                               '[{"name": "Световой поток", "value": 6200},], "notes": "проверить яркость",}]',
                    "finish_reason": "stop"}
        if system.startswith("Ты — инженер-сметчик"):
            self.calls.append("estimate")
            return {"content": 'Оценка:\n{"equipment": [{"name": "Лазерный проектор", "quantity": "1 шт", "unit": "шт", '
                               '"key_specs": "6000 лм", "unit_cost_min": "180 000,00", "unit_cost_max": "195 000 руб.", '
                               '"price_basis": "оценка ИИ"}], "installation_required": "да", "installation_scope": "монтаж и настройка", '
                               '"installation_cost_min": "15 тыс.", "installation_cost_max": null, "other_costs": "доставка", '
                               '"risks": "сжатые сроки поставки", "recommendation": "Участвовать.", "reasoning": "маржа есть",}',
                    "finish_reason": "stop"}
        if system.startswith("Ты — аналитик отдела тендеров"):
            self.calls.append("score")
            # номер числом (потерян ведущий ноль), «№ …», баллы строками; третью закупку модель «забыла»
            return {"content": "Вот оценка {по схеме}:\n```json\n{\"scores\": [{\"number\": 318100005226000001, \"score\": \"85%\", "
                               "\"reason\": \"лазерный проектор — наш профиль\"}, {\"number\": \"№ 32616576327\", \"score\": \"70\", "
                               "\"reason\": \"проектор\"},]}\n```", "finish_reason": "stop"}
        raise AssertionError(system[:80])


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(procurement, "DELAY_BETWEEN_REQUESTS", 0)
    docs = tmp_path / "Документы"
    monkeypatch.setattr("jarvis_app.tender.documents_dir", lambda: docs)
    monkeypatch.setattr("jarvis_app.analyst.documents_dir", lambda: docs)
    giga = FakeGigaChat()
    monkeypatch.setattr(cloud_llm.GigaChat, "_post", lambda self, body: giga.post(body))
    monkeypatch.setattr(cloud_llm.GigaChat, "_access_token", lambda self: "TOKEN")
    config = Config(tmp_path / "config.json")
    config.update({"ai_provider": "gigachat", "gigachat_model": "GigaChat-2",
                   "analyst": {"profile": "Поставка и монтаж проекторов, звукового и сценического оборудования",
                               "min_score": 60, "top_n": 3}})
    sessions: list[FakeEIS] = []

    def client() -> ProcurementClient:
        session = FakeEIS()
        sessions.append(session)
        return ProcurementClient(session=session)

    return {"config": config, "client": client, "giga": giga, "docs": docs, "sessions": sessions, "tmp": tmp_path}


def _search(env) -> tuple[list, list[str]]:
    return env["client"]().search_tags(["проектор"], strict=True, minus_words=list(DEFAULT_MINUS_WORDS), only_active=True)


def test_search_strict_minus_and_stale_filters(env):
    items, notes = _search(env)
    by_number = {p.number: p for p in items}
    assert set(by_number) == {N44, N223, N_OTHER}
    p44 = by_number[N44]
    assert p44.title == "Лазерный проектор" and p44.price == 218667.0
    assert (p44.law, p44.method, p44.status) == ("44-ФЗ", "Запрос котировок в электронной форме", "Подача заявок")
    assert p44.customer == "ФГБНУ «КРАСНОЯРСКИЙ НАУЧНЫЙ ЦЕНТР»" and p44.url == CARD44 and p44.tags == ["проектор"]
    assert by_number[N223].title == "Поставка проектора" and by_number[N223].url == CARD223
    assert by_number[N223].price == 2133516.0
    assert "Скрыто не по теме: 1 (в названии нет слов тега)" in notes
    assert any(n.startswith("Скрыто по минус-словам: 1 («медицин» — 1)") for n in notes)
    assert any(n.startswith("Скрыто неактуальных закупок: 2") for n in notes)


def _events(ws_events, name):
    return [p for e, p in ws_events if e == name]


def test_pipeline_44fz_end_to_end(env):
    items, _ = _search(env)
    purchase = next(p for p in items if p.number == N44).to_dict()
    events: list = []
    ws = TenderWorkspace(env["config"], lambda e, p: events.append((e, p)))
    ws.set_purchase(purchase)
    reports: list = []
    events.clear()
    client = env["client"]()
    ws.run_pipeline(client, reports.append)
    state = ws.state
    steps = {k: v["status"] for k, v in state["steps"].items()}
    assert steps == {"files": "done", "analysis": "done", "match": "done", "estimate": "done", "docs": "done"}, state["steps"]

    asked = env["sessions"][-1].asked
    assert not any("signview" in u or "listModal" in u for u in asked)  # подписи не скачиваем
    files = {f["name"]: f for f in state["files"]}
    names = set(files)
    assert "Приложение № 3 НМЦК.xlsx" in names and "Приложение № 3 НМЦК.pdf" in names
    assert "Извещение о проведении запроса котировок.rtf" in names  # имя из Content-Disposition (RFC 5987)
    assert not any("(2)" in n for n in names)  # прежняя редакция с теми же байтами не сохранена второй копией
    doc = files["Приложение № 4 Требования к заявке.doc"]
    assert doc["kind"] == "ok" and doc["status"] == "текст извлечён" and doc["chars"] > 100
    assert files["Уведомление Минпромторг.pdf"]["kind"] == "warning"
    assert files["Обоснование цены контракта.docx"]["status"] == "текст извлечён"  # имя из cp866 в архиве
    assert files["Обоснование цены контракта.docx.sig"]["kind"] == "skipped"
    assert files["Документация.zip"]["status"].startswith("архив: 2")
    assert len(state["download_errors"]) == 1 and "Приложение № 5 Форма заявки.docx" in state["download_errors"][0]
    assert "Доступ ограничен" in state["download_errors"][0]
    assert "не скачались: 1" in state["steps"]["files"]["note"]

    text = (ws.folder() / "tz_text.txt").read_text(encoding="utf-8")
    assert text.index("Описание объекта закупки") < text.index("Требования к заявке") < text.index("Типовой контракт")
    assert "Файл: Приложение № 3 НМЦК.xlsx" in text and "Файл: Приложение № 3 НМЦК.pdf" not in text
    assert "декларацию о стране происхождения товара («Россия»" in text  # .doc прочитан без Word, кавычки целы
    assert "Выписка из реестра российской промышленной продукции | да" in text
    assert "Общие положения" not in text and "Оплата в течение 7 рабочих дней" in text  # контракт — выжимкой

    analysis = state["analysis"]
    assert env["giga"].calls.count("analysis") >= 3  # обрыв → повтор, плюс остальные части ТЗ
    assert analysis["failed_parts"] == 0
    assert analysis["object"] == "Поставка «Лазерного проектора»"
    assert analysis["restrictions"] == ["ПП РФ № 1875", "Нацрежим"]
    assert analysis["penalties"] == "штраф; пени" and analysis["contract_security"] == "5"
    assert analysis["participant_requirements"] == ["СМП"]
    [pos] = analysis["positions"]
    assert (pos["name"], pos["quantity"], pos["okpd2"]) == ("Лазерный проектор", "1", "26.2")
    assert [c["name"] for c in pos["characteristics"]] == ["Световой поток", "Разрешение", "Срок службы источника"]
    assert pos["characteristics"][2]["requirement"] == "20000"

    [match] = state["matches"]
    assert (match["brand"], match["model"], match["compliance"]) == ("Epson", "EB-L630U", "соответствует")
    assert match["offered_values"] == [{"name": "Световой поток", "value": "6200"}]

    est, totals = state["estimate"], state["totals"]
    assert est["installation_required"] is True and est["risks"] == ["сжатые сроки поставки"]
    assert est["recommendation"] == "участвовать" and est["equipment"][0]["quantity"] == 1
    assert (totals["equipment_min"], totals["equipment_max"]) == (180000.0, 195000.0)
    assert (totals["installation_min"], totals["installation_max"]) == (15000.0, 15000.0)
    assert (totals["cost_min"], totals["cost_max"]) == (195000.0, 210000.0)
    assert (totals["margin_min"], totals["margin_max"]) == (8667.0, 23667.0)
    assert "маржа 4.0…10.8%" in state["steps"]["estimate"]["note"]

    assert len(state["documents"]) == 3 and all(Path(p).exists() for p in state["documents"])
    assert reports and reports[0]["totals"]["cost_min"] == 195000.0
    # интерфейс: во время конвейера «занято» не снимается, только последним сообщением
    published = _events(events, "tender_state")
    assert published and all(p["busy"] for p in published[:-1]) and published[-1]["busy"] is False
    assert published[-1]["status"].startswith("Готово")


def test_pipeline_223fz_old_card_and_link_text_names(env):
    ws = TenderWorkspace(env["config"], lambda e, p: None)
    ws.set_purchase({"number": N223, "url": CARD223, "title": "Поставка проектора", "price": 2133516.0, "law": "223-ФЗ"})
    ws.run_pipeline(env["client"]())
    steps = {k: v["status"] for k, v in ws.state["steps"].items()}
    assert steps == {"files": "done", "analysis": "done", "match": "done", "estimate": "done", "docs": "done"}, ws.state["steps"]
    assert DOCS223 in env["sessions"][-1].asked  # «&noticeGuid» не превратился в «¬iceGuid»
    names = sorted(f["name"] for f in ws.state["files"])
    assert names == ["Извещение о закупке.docx", "Приложение 1 Расчёт цены.xlsx", "Проект договора.docx",
                     "Техническое задание на поставку проектора.docx"]
    assert all(f["status"] == "текст извлечён" for f in ws.state["files"])
    assert ws.state["analysis"]["positions"][0]["quantity"] == "2"


def test_analyst_scores_then_deep_reports(env):
    items, _ = _search(env)
    notes: list = []
    statuses: list = []
    analyst = an.Analyst(env["config"], env["client"], lambda e, p: statuses.append((e, p)),
                         notify=lambda title, text: notes.append((title, text)),
                         store=an.AnalystStore(env["tmp"] / "analyst.json"))
    reports = analyst.run(items)
    assert [r["purchase"]["number"] for r in reports] == [N44, N223]  # по убыванию баллов; третью ИИ пропустил
    assert analyst.store.scores[N44]["score"] == 85 and analyst.store.scores[N223]["score"] == 70
    assert N_OTHER not in analyst.store.scores
    r44 = reports[0]
    assert r44["error"] == "" and r44["totals"]["margin_min"] == 8667.0
    assert any("Доступ ограничен" in w for w in r44["warnings"])
    text = an.format_report(r44)
    assert "Соответствие профилю: 85/100" in text and "Маржа к НМЦК" in text and "Рекомендация: УЧАСТВОВАТЬ" in text
    assert (env["docs"] / "Аналитика" / N44 / "Отчёт.txt").exists()
    assert len(notes) == 2 and notes[0][0] == "Интересная закупка (85/100)"
    assert statuses[-1][1]["busy"] is False and "разобрано подробно 2" in statuses[-1][1]["text"]
    # повторный запуск: всё уже оценено и разобрано — ИИ не вызывается
    before = list(env["giga"].calls)
    assert analyst.run(items) == [] and env["giga"].calls.count("score") == before.count("score") + 1  # только «забытая»


def test_diagnose_reports_every_failure_with_reason(env, monkeypatch, capsys):
    import diagnose

    monkeypatch.setattr(diagnose, "documents_dir", lambda: env["docs"])
    monkeypatch.setattr(diagnose, "Config", lambda: env["config"])
    monkeypatch.setattr(diagnose, "ProcurementClient", lambda ca_bundle="": env["client"]())
    code = diagnose.main([N44, "--ai"])
    out = capsys.readouterr().out
    fails = [line for line in out.splitlines() if line.startswith("[FAIL]")]
    assert code == 1 and len(fails) == 1, out
    assert re.match(r"^\[FAIL\] скачивание файла: Приложение № 5 Форма заявки\.docx: ЕИС вместо страницы показала заглушку", fails[0])
    assert "ошибок: 1\n  1. 3. Карточка и документы закупки → скачивание файла:" in out
    assert "[WARN] Уведомление Минпромторг.pdf: текст не найден" in out
    assert "[OK]   Приложение № 4 Требования к заявке.doc: текст извлечён" in out
    assert "[OK]   расчёт" in out and "себестоимость 195,000–210,000 ₽" in out
