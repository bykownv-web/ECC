from datetime import date

import pytest

from jarvis_app import procurement as pr

SAMPLE_HTML = """
<html><body>
<div class="search-registry-entry-block box-shadow-search-input">
  <div class="registry-entry__header">
    <div class="registry-entry__header-top">
      <div class="d-flex registry-entry__header-top__title text-truncate">44-ФЗ<br>Электронный аукцион</div>
    </div>
    <div class="registry-entry__header-mid align-items-center">
      <div class="registry-entry__header-mid__number">
        <a href="/epz/order/notice/ea20/view/common-info.html?regNumber=0373100000126000001" target="_blank">№ 0373100000126000001</a>
      </div>
      <div class="registry-entry__header-mid__title text-normal">Подача заявок</div>
    </div>
  </div>
  <div class="registry-entry__body">
    <div class="registry-entry__body-block">
      <div class="registry-entry__body-title">Объект закупки</div>
      <div class="registry-entry__body-value">Поставка мультимедийного проектора для актового зала</div>
    </div>
    <div class="registry-entry__body-block">
      <div class="registry-entry__body-title">Заказчик</div>
      <div class="registry-entry__body-href"><a href="/epz/organization/view/info.html?organizationCode=1">ГБОУ ШКОЛА № 1</a></div>
    </div>
  </div>
  <div class="price-block">
    <div class="price-block__title">Начальная цена</div>
    <div class="price-block__value">1 234 567,89&nbsp;₽</div>
  </div>
  <div class="data-block mt-auto">
    <div class="row">
      <div class="col-6"><div class="data-block__title">Размещено</div><div class="data-block__value">10.11.2026</div></div>
      <div class="col-6"><div class="data-block__title">Обновлено</div><div class="data-block__value">12.11.2026</div></div>
    </div>
    <div class="data-block__title">Окончание подачи заявок</div>
    <div class="data-block__value">19.11.2026</div>
  </div>
</div>
<div class="search-registry-entry-block">
  <div class="registry-entry__header-top__title">223-ФЗ<br>Запрос котировок</div>
  <div class="registry-entry__header-mid__number"><a href="/epz/order/notice/notice223/common-info.html?regNumber=32615000001">№ 32615000001</a></div>
  <div class="registry-entry__header-mid__title">Подача заявок</div>
  <div class="registry-entry__body-title">Объект закупки</div>
  <div class="registry-entry__body-value">Звуковое оборудование для театра</div>
  <div class="registry-entry__body-title">Организация, осуществляющая размещение</div>
  <div class="registry-entry__body-href"><a href="#">ГАУК «ТЕАТР ДРАМЫ»</a></div>
  <div class="price-block__value">500 000,00 ₽</div>
  <div class="data-block__title">Окончание подачи заявок</div>
  <div class="data-block__value">30.11.2026</div>
</div>
<div class="search-registry-entry-block"><p>битая карточка без номера</p></div>
</body></html>
"""


def test_parse_results_extracts_fields():
    items = pr.parse_results(SAMPLE_HTML)
    assert len(items) == 2
    first, second = items
    assert first.number == "0373100000126000001"
    assert first.url.startswith("https://zakupki.gov.ru/epz/order/notice/ea20/")
    assert first.law == "44-ФЗ"
    assert first.method == "Электронный аукцион"
    assert first.status == "Подача заявок"
    assert first.title == "Поставка мультимедийного проектора для актового зала"
    assert first.customer == "ГБОУ ШКОЛА № 1"
    assert first.price == pytest.approx(1234567.89)
    assert (first.published, first.updated, first.deadline) == ("10.11.2026", "12.11.2026", "19.11.2026")
    assert second.law == "223-ФЗ"
    assert second.customer == "ГАУК «ТЕАТР ДРАМЫ»"
    assert second.price == 500000.0


@pytest.mark.parametrize(
    "text,expected",
    [("1 234 567,89 ₽", 1234567.89), ("500\xa0000,00", 500000.0), ("", None), ("—", None), ("12.50", 12.5)],
)
def test_parse_price(text, expected):
    assert pr.parse_price(text) == (pytest.approx(expected) if expected else expected)


def test_build_search_url_flags():
    url = pr.build_search_url(pr.SearchParams(query="проектор", fz223=False, price_from=1000))
    assert "fz44=on" in url and "fz223" not in url
    assert "af=on" in url and "priceFromGeneral=1000" in url
    assert "searchString=%D0%BF%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D0%BE%D1%80" in url
    with pytest.raises(pr.ProcurementError):
        pr.build_search_url(pr.SearchParams(query="x", fz44=False, fz223=False))


class FakeClient(pr.ProcurementClient):
    def __init__(self, pages):
        super().__init__()
        self.pages = pages

    def _get(self, url):
        for key, html in self.pages.items():
            if key in url:
                return html
        raise pr.ProcurementError("нет страницы")


def test_search_tags_merges_duplicates_and_reports_errors():
    client = FakeClient({"%D0%BF%D1%80%D0%BE": SAMPLE_HTML, "%D0%B7%D0%B2": SAMPLE_HTML})
    items, errors = client.search_tags(["проектор", "звук", "театр"], only_active=False)
    assert len(items) == 2
    assert items[0].tags == ["проектор", "звук"]
    assert len(errors) == 1 and errors[0].startswith("театр")


def test_analyze_and_export(tmp_path):
    items = pr.parse_results(SAMPLE_HTML)
    items[0].tags = ["проектор"]
    stats = pr.analyze(items, today=date(2026, 11, 17))
    assert stats["count"] == 2
    assert stats["total_sum"] == pytest.approx(1734567.89)
    assert stats["by_law"] == {"44-ФЗ": 1, "223-ФЗ": 1}
    assert [d["number"] for d in stats["deadline_soon"]] == ["0373100000126000001"]
    assert stats["top_customers"][0]["customer"] == "ГБОУ ШКОЛА № 1"

    path = pr.export_to_excel(items, tmp_path / "out.xlsx")
    from openpyxl import load_workbook

    wb = load_workbook(path)
    ws = wb["Закупки"]
    assert ws.max_row == 3
    assert ws["A2"].value == "0373100000126000001"
    assert "Сводка" in wb.sheetnames


def test_seen_store_reports_only_new(tmp_path):
    store = pr.SeenStore(tmp_path / "seen.json")
    items = pr.parse_results(SAMPLE_HTML)
    assert store.is_empty()
    assert len(store.filter_new(items)) == 2
    assert store.filter_new(items) == []
    assert pr.SeenStore(tmp_path / "seen.json").filter_new(items) == []


def test_summary_text_mentions_numbers():
    text = pr.summary_text(pr.parse_results(SAMPLE_HTML))
    assert "Найдено закупок: 2" in text
    assert "0373100000126000001" in text


def test_is_actual_filters_stale_and_wrong_stage():
    today = date(2026, 11, 18)
    ok = pr.Purchase(number="1", url="u", status="Подача заявок", deadline="20.11.2026")
    past = pr.Purchase(number="2", url="u", status="Подача заявок", deadline="10.03.2024")
    closed = pr.Purchase(number="3", url="u", status="Работа комиссии", deadline="25.11.2026")
    no_date_old = pr.Purchase(number="4", url="u", status="Подача заявок", published="01.02.2023")
    no_date_new = pr.Purchase(number="5", url="u", status="Подача заявок", updated="10.11.2026")
    assert pr.is_actual(ok, today)
    assert not pr.is_actual(past, today)
    assert not pr.is_actual(closed, today)
    assert not pr.is_actual(no_date_old, today)
    assert pr.is_actual(no_date_new, today)


def test_search_tags_hides_stale_when_only_active(monkeypatch):
    stale = SAMPLE_HTML.replace("19.11.2026", "19.11.2020").replace("30.11.2026", "30.11.2020")
    client = FakeClient({"%D0%BF%D1%80%D0%BE": stale})
    items, errors = client.search_tags(["проектор"], only_active=True)
    assert items == []
    assert any("неактуальных закупок: 2" in e for e in errors)
    items, _ = client.search_tags(["проектор"], only_active=False)
    assert len(items) == 2


def test_search_tags_strict_and_minus_words():
    html = SAMPLE_HTML.replace("Поставка мультимедийного проектора для актового зала", "Поставка медицинского проектора для операционной")
    client = FakeClient({"%D0%BF%D1%80%D0%BE": html})
    items, errors = client.search_tags(["проектор"], only_active=False, strict=True, minus_words=["медицин"])
    assert items == []
    assert any("не по теме: 1" in e for e in errors)  # «Звуковое оборудование для театра» — не про проекторы
    assert any("минус-словам: 1" in e and "медицин" in e for e in errors)


def test_content_disposition_names():
    from urllib.parse import quote

    name = "Приложение № 3 НМЦК.xlsx"
    f = pr.filename_from_disposition
    assert f("attachment; filename*=UTF-8''" + quote(name)) == name
    assert f("attachment; filename*=\"UTF-8''" + quote(name) + "\"") == name
    assert f("attachment; filename*=windows-1251''" + quote(name, encoding="cp1251")) == name
    assert f('attachment; filename="' + name.encode("cp1251").decode("latin-1") + '"') == name
    assert f('attachment; filename="' + name.encode("utf-8").decode("latin-1") + '"') == name
    assert f('attachment; filename="x.xlsx"; filename*=UTF-8\'ru\'' + quote(name)) == name


def test_html_instead_of_file_is_reported():
    import requests

    class S:
        headers = {}

        def get(self, url, timeout=None, verify=None):
            r = requests.Response()
            r.status_code, r._content = 200, "<html><title>Доступ ограничен</title></html>".encode()
            r.headers = requests.structures.CaseInsensitiveDict({"Content-Type": "text/html"})
            return r

    client = pr.ProcurementClient(session=S())
    pr.DELAY_BETWEEN_REQUESTS, old = 0, pr.DELAY_BETWEEN_REQUESTS
    try:
        with pytest.raises(pr.ProcurementError, match="заглушку"):
            client.get_file("https://zakupki.gov.ru/44fz/filestore/x")
        with pytest.raises(pr.ProcurementError, match="Доступ ограничен"):
            client.search(pr.SearchParams(query="проектор"))
    finally:
        pr.DELAY_BETWEEN_REQUESTS = old
