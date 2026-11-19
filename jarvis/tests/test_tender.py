import json
import zipfile
from pathlib import Path

import pytest

from jarvis_app import doc_builder, tender_ai, tender_docs
from jarvis_app.config import Config
from jarvis_app.llm import LLM, LLMError, _loads
from jarvis_app.tender import TenderWorkspace


def make_docx(path: Path) -> Path:
    import docx

    d = docx.Document()
    d.add_paragraph("Техническое задание на поставку проекторов")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Световой поток", "не менее 6000 лм"
    t.cell(1, 0).text, t.cell(1, 1).text = "Количество", "2 шт"
    d.save(str(path))
    return path


def test_extract_docx_xlsx_txt_rtf(tmp_path):
    text = tender_docs.extract_text(make_docx(tmp_path / "tz.docx"))
    assert "Световой поток | не менее 6000 лм" in text

    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["Позиция", "Кол-во"])
    wb.active.append(["Экран", 2])
    wb.save(tmp_path / "spec.xlsx")
    assert "Экран | 2" in tender_docs.extract_text(tmp_path / "spec.xlsx")

    (tmp_path / "a.txt").write_bytes("Пульт ДУ".encode("cp1251"))
    assert tender_docs.extract_text(tmp_path / "a.txt") == "Пульт ДУ"

    (tmp_path / "b.rtf").write_text(r"{\rtf1\ansi {\b \'cf\'f0\'ee\'e5\'ea\'f2\'ee\'f0}\par}", encoding="ascii")
    assert "Проектор" in tender_docs.extract_text(tmp_path / "b.rtf")

    # .doc без Word: текст достаётся из бинарника (UTF-16)
    body = "Требования к содержанию заявки: декларация о стране происхождения товара".encode("utf-16-le")
    (tmp_path / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 64 + body + b"\x00" * 32)
    assert "декларация о стране происхождения" in tender_docs.extract_text(tmp_path / "old.doc")
    (tmp_path / "bad.doc").write_bytes(b"\x00\x01\x02" * 50)
    with pytest.raises(ValueError, match="docx"):
        tender_docs.extract_text(tmp_path / "bad.doc")


def test_zip_is_unpacked_safely_and_processed(tmp_path):
    archive = tmp_path / "docs.zip"
    inner = make_docx(tmp_path / "inner.docx")
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(inner, "../../evil/ТЗ.docx")
    files, text = tender_docs.process_files([archive], tmp_path / "work")
    assert files[0].status.startswith("архив")
    assert any(f.name == "ТЗ.docx" and f.status == "текст извлечён" for f in files)
    assert "6000 лм" in text
    assert not (tmp_path.parent / "evil").exists()


def test_eis_urls_and_links():
    url = "https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=0119300019826002251"
    assert tender_docs.documents_page_url(url) == url.replace("common-info.html", "documents.html")
    html = """<a href="https://zakupki.gov.ru/44fz/filestore/public/1.0/download/priz/file.html?uid=AB12" title="ТЗ.docx">ТЗ</a>
              <a href="/epz/order/notice/ea20/view/supplier-results.html">не файл</a>
              <a href="https://evil.example/download?uid=1">чужой</a>"""
    html += """<a href="https://zakupki.gov.ru/epz/order/notice/signview/ep/listModal.html?reestrNumber=1&uid=AB12">подпись</a>"""
    links = tender_docs.parse_document_links(html, url)
    assert links == [("ТЗ.docx", "https://zakupki.gov.ru/44fz/filestore/public/1.0/download/priz/file.html?uid=AB12")]
    assert tender_docs.purchase_url_from_input("http://zakupki.gov.ru/x?regNumber=1").startswith("https://")
    assert tender_docs.purchase_url_from_input("https://evil.example/") is None
    assert tender_docs.safe_name('..\\a:b*?.docx') == "_a_b__.docx"


def test_chunk_and_merge():
    text = "\n".join(f"строка {i} " + "x" * 50 for i in range(600))
    chunks = tender_ai.chunk_text(text, size=5000)
    assert len(chunks) > 5 and all(len(c) <= 5000 for c in chunks)
    merged = tender_ai.merge_analyses([
        {"summary": "Проекторы", "restrictions": ["ПП 1875"], "positions": [
            {"name": "Проектор", "quantity": "2", "unit": "шт", "okpd2": "", "characteristics": [{"name": "Яркость", "requirement": "≥ 6000"}]}]},
        {"summary": "", "restrictions": ["пп 1875", "СМП"], "positions": [
            {"name": "проектор ", "quantity": "", "unit": "", "okpd2": "26.70", "characteristics": [{"name": "HDMI", "requirement": "2"}, {"name": "яркость", "requirement": "x"}]}]},
    ])
    assert merged["summary"] == "Проекторы"
    assert merged["restrictions"] == ["ПП 1875", "СМП"]
    assert len(merged["positions"]) == 1
    pos = merged["positions"][0]
    assert pos["okpd2"] == "26.70" and [c["name"] for c in pos["characteristics"]] == ["Яркость", "HDMI"]


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def json(self, system, prompt, schema):
        self.calls.append((system, prompt))
        return self.replies.pop(0)


ANALYSIS = {
    "summary": "Поставка проекторов", "object": "", "delivery_place": "Красноярск", "delivery_terms": "", "deadline": "",
    "bid_security": "1%", "contract_security": "", "warranty": "", "participant_requirements": [],
    "documents_required": ["Декларация"], "restrictions": [],
    "positions": [{"name": "Проектор лазерный", "quantity": "2", "unit": "шт", "okpd2": "",
                   "characteristics": [{"name": "Световой поток", "requirement": "не менее 6000 лм"}]}],
}


def test_match_uses_catalog_data_not_llm_inventions():
    catalog = [
        {"id": "1", "name": "Проектор лазерный Epson EB-L630U", "brand": "Epson", "model": "EB-L630U", "price": "450000", "country": "Филиппины"},
        {"id": "2", "name": "Экран Lumien", "brand": "Lumien", "model": "MC", "price": "90000", "country": "Россия"},
    ]
    llm = FakeLLM([{"matches": [{"position_index": 0, "catalog_id": "1", "brand": "Выдумка", "model": "X", "country": "",
                                 "compliance": "соответствует", "price": "1", "offered_values": [{"name": "Световой поток", "value": "6200 лм"}], "notes": ""}]}])
    res = tender_ai.match_equipment(llm, ANALYSIS, catalog)
    assert res[0]["model"] == "EB-L630U" and res[0]["brand"] == "Epson" and res[0]["price"] == "450000"
    assert res[0]["from_catalog"] is True
    assert "Epson" in llm.calls[0][1]

    llm = FakeLLM([{"matches": [{"position_index": 0, "catalog_id": "999", "brand": "B", "model": "M", "country": "",
                                 "compliance": "соответствует", "price": "", "offered_values": [], "notes": ""}]}])
    res = tender_ai.match_equipment(llm, ANALYSIS, catalog)
    assert res[0]["compliance"] == "не найдено" and res[0]["model"] == ""


def test_free_match_flags_for_review():
    llm = FakeLLM([{"matches": [{"position_index": 0, "catalog_id": "", "brand": "Epson", "model": "EB-L630U", "country": "Филиппины",
                                 "compliance": "соответствует", "price": "", "offered_values": [], "notes": "проверить яркость"}]}])
    res = tender_ai.match_equipment(llm, ANALYSIS, None)
    assert res[0]["from_catalog"] is False and res[0]["notes"].startswith("Проверить")


def test_catalog_xlsx(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Прайс-лист ООО Пример"])
    ws.append(["Наименование", "Производитель", "Модель", "Цена, руб", "Страна"])
    ws.append(["Проектор", "Epson", "EB-L630U", 450000, "Филиппины"])
    ws.append([None, None, None, None, None])
    wb.save(tmp_path / "price.xlsx")
    items = tender_ai.load_catalog_xlsx(tmp_path / "price.xlsx")
    assert items == [{"name": "Проектор", "brand": "Epson", "model": "EB-L630U", "price": "450000", "country": "Филиппины", "id": "1"}]


def test_documents_are_built(tmp_path):
    purchase = {"number": "0119", "law": "44-ФЗ", "title": "Проекторы", "customer": "Школа", "price": 1000000.0, "url": "https://zakupki.gov.ru/x"}
    matches = [{"position_index": 0, "position": "Проектор лазерный", "quantity": "2", "unit": "шт", "brand": "Epson", "model": "EB-L630U",
                "country": "Филиппины", "compliance": "соответствует", "price": "450 000,00", "offered_values": [{"name": "Световой поток", "value": "6200 лм"}],
                "notes": "", "from_catalog": True}]
    company = {"name": "ООО Пример", "inn": "7700000000", "director": "Иванов И.И."}
    offer = doc_builder.build_offer(tmp_path / "o.docx", purchase, ANALYSIS, matches, company)
    kp = doc_builder.build_commercial(tmp_path / "k.docx", purchase, matches, company)
    chk = doc_builder.build_checklist(tmp_path / "c.docx", purchase, ANALYSIS, matches)
    texts = {p.name: tender_docs.extract_text(p) for p in (offer, kp, chk)}
    assert "Световой поток: 6200 лм" in texts["o.docx"] and "Epson EB-L630U" in texts["o.docx"]
    assert "Итого: 900 000,00 ₽" in texts["k.docx"] and "снижение 10.0%" in texts["k.docx"]
    assert "☐ Декларация" in texts["c.docx"] and "Красноярск" in texts["c.docx"]


def test_workspace_end_to_end(tmp_path, monkeypatch):
    events = []
    config = Config(tmp_path / "c.json")
    ws = TenderWorkspace(config, lambda e, p: events.append((e, p)))
    monkeypatch.setattr("jarvis_app.tender.documents_dir", lambda: tmp_path / "docs")
    ws.set_purchase({"number": "0119300019826002251", "title": "Проекторы", "url": "https://zakupki.gov.ru/x", "price": 1000000.0})
    ws.add_files([str(make_docx(tmp_path / "tz.docx"))])
    assert ws.state["text_chars"] > 0 and ws.state["files"][0]["status"] == "текст извлечён"

    fake = FakeLLM([ANALYSIS, {"matches": [{"position_index": 0, "catalog_id": "", "brand": "Epson", "model": "EB-L630U", "country": "",
                                           "compliance": "частично", "price": "", "offered_values": [], "notes": ""}]}])
    monkeypatch.setattr("jarvis_app.tender.LLM", lambda cfg: fake)
    ws.analyze()
    ws.match()
    ws.build_documents()
    assert len(ws.state["documents"]) == 3 and all(Path(p).exists() for p in ws.state["documents"])
    saved = json.loads((ws.folder() / "state.json").read_text(encoding="utf-8"))
    assert saved["matches"][0]["model"] == "EB-L630U"
    # повторный выбор той же закупки продолжает сохранённую работу
    ws2 = TenderWorkspace(config, lambda e, p: None)
    ws2.set_purchase({"number": "0119300019826002251", "title": "Проекторы"})
    assert ws2.state["analysis"]["summary"] == "Поставка проекторов"
    assert any(e == "tender_state" for e, _ in events)


def test_llm_json_parsing_and_provider_none(tmp_path):
    assert _loads('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(LLMError):
        _loads("не json")
    config = Config(tmp_path / "c.json")
    config.update({"ai_provider": "none"})
    with pytest.raises(LLMError, match="нужен ИИ"):
        LLM(config).json("s", "p", {})


def test_ollama_json_request_shape(tmp_path):
    class Resp:
        status_code = 200

        def json(self):
            return {"message": {"content": '{"ok": true}'}}

    class Sess:
        def post(self, url, json=None, timeout=None):
            self.body = json
            return Resp()

    sess = Sess()
    config = Config(tmp_path / "c.json")
    assert LLM(config, session=sess).json("SYS", "PROMPT", {"type": "object"}) == {"ok": True}
    assert sess.body["format"] == {"type": "object"} and sess.body["options"]["num_ctx"] >= 8192


def test_pipeline_runs_all_steps_and_reports(tmp_path, monkeypatch):
    from jarvis_app import analyst as an

    monkeypatch.setattr("jarvis_app.tender.documents_dir", lambda: tmp_path / "docs")
    config = Config(tmp_path / "c.json")
    ws = TenderWorkspace(config, lambda e, p: None)
    ws.set_purchase({"number": "0119300019826002251", "title": "Проекторы", "url": "https://zakupki.gov.ru/x", "price": 2_000_000.0})
    tz = make_docx(tmp_path / "Техническое задание.docx")
    monkeypatch.setattr("jarvis_app.tender.tender_docs.fetch_eis_documents", lambda client, url, dest: [tz])
    estimate = {"equipment": [{"name": "Проектор", "quantity": 2, "unit": "шт", "key_specs": "", "unit_cost_min": 400000,
                               "unit_cost_max": 500000, "price_basis": "оценка ИИ"}],
                "installation_required": False, "installation_scope": "", "installation_cost_min": 0, "installation_cost_max": 0,
                "other_costs": "", "risks": [], "recommendation": "участвовать", "reasoning": ""}

    def handler(system, prompt):
        if system == an.ESTIMATE_SYSTEM:
            return estimate
        if system.startswith("Ты — специалист по госзакупкам"):
            return ANALYSIS
        return {"matches": [{"position_index": 0, "catalog_id": "", "brand": "Epson", "model": "EB-L630U", "country": "",
                             "compliance": "соответствует", "price": "", "offered_values": [], "notes": ""}]}

    fake = FakeLLM([])
    fake.json = lambda system, prompt, schema: handler(system, prompt)
    monkeypatch.setattr("jarvis_app.tender.LLM", lambda cfg: fake)
    monkeypatch.setattr("jarvis_app.analyst.LLM", lambda cfg: fake)
    reports = []
    ws.run_pipeline(client=None, on_report=reports.append)
    steps = {k: v["status"] for k, v in ws.state["steps"].items()}
    assert steps == {"files": "done", "analysis": "done", "match": "done", "estimate": "done", "docs": "done"}
    assert ws.state["totals"]["cost_max"] == 1_000_000 and ws.state["totals"]["margin_min"] == 1_000_000
    assert len(ws.state["documents"]) == 3
    assert reports and reports[0]["purchase"]["number"] == "0119300019826002251"


def test_pipeline_stops_on_missing_docs_and_is_cancelled_by_new_purchase(tmp_path, monkeypatch):
    from jarvis_app.procurement import ProcurementError

    monkeypatch.setattr("jarvis_app.tender.documents_dir", lambda: tmp_path / "docs")
    ws = TenderWorkspace(Config(tmp_path / "c.json"), lambda e, p: None)
    ws.set_purchase({"number": "1", "url": "https://zakupki.gov.ru/x"})

    def no_docs(client, url, dest):
        raise ProcurementError("файлов нет.")

    monkeypatch.setattr("jarvis_app.tender.tender_docs.fetch_eis_documents", no_docs)
    ws.run_pipeline(client=None)
    assert ws.state["steps"]["files"]["status"] == "error" and "Выбрать файлы" in ws.state["steps"]["files"]["note"]


def test_analysis_text_prioritizes_tz_and_trims_contract():
    contract = "\n".join(["Общие положения договора " * 5] * 400 + ["Срок поставки: 30 дней", "Оплата в течение 7 дней"])
    text = tender_docs.build_analysis_text([("Проект контракта.docx", contract), ("ТЗ.docx", "Проектор 6000 лм")])
    assert text.index("ТЗ.docx") < text.index("Проект контракта.docx")
    assert "Срок поставки: 30 дней" in text and "Общие положения" not in text


class FakeEIS:
    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    def get_text(self, url):
        from jarvis_app.procurement import ProcurementError

        self.asked.append(url)
        if url not in self.pages:
            raise ProcurementError("ЕИС вернула код 404.")
        return self.pages[url]

    def get_file(self, url):
        return b"%PDF-1.4 fake", "ТЗ.pdf"


def test_223_documents_tab_is_taken_from_card(tmp_path):
    card = "https://zakupki.gov.ru/epz/order/notice/notice223/common-info.html?regNumber=32616576327"
    tab = "https://zakupki.gov.ru/epz/order/notice/notice223/documents.html?noticeInfoId=18447125"
    eis = FakeEIS({
        card: '<a href="/epz/order/notice/notice223/documents.html?noticeInfoId=18447125">Документы</a>',
        tab: '<a href="https://zakupki.gov.ru/223/purchase/public/download/download.html?id=555" title="ТЗ.pdf">ТЗ</a>',
    })
    files = tender_docs.fetch_eis_documents(eis, card, tmp_path)
    assert [f.name for f in files] == ["ТЗ.pdf"]
    assert tab in eis.asked and tender_docs.documents_page_url(card) not in eis.asked


def test_title_highlight_is_glued():
    from jarvis_app.procurement import parse_results

    html = """<div class="search-registry-entry-block">
      <div class="registry-entry__header-mid__number"><a href="/x?regNumber=32616576327">№ 32616576327</a></div>
      <div class="registry-entry__body-title">Объект закупки</div>
      <div class="registry-entry__body-value">Поставка <span class="highlight">проектор</span>а и <b>экран</b>ов</div>
    </div>"""
    assert parse_results(html)[0].title == "Поставка проектора и экранов"


def test_old_223_card_with_unescaped_ampersand_entity(tmp_path):
    card = "https://zakupki.gov.ru/223/purchase/public/purchase/info/common-info.html?regNumber=32616576327"
    tab = ("https://zakupki.gov.ru/epz/order/notice/notice223/documents.html?purchaseNoticeNumber=32616576327"
           "&noticeGuid=38f2fc7f-c0f9-48d6-b181-e4386a620809")
    # в HTML ЕИС амперсанд не экранирован: «&noticeGuid» — html.parser видит в нём «&not»
    card_html = '<a href="/epz/order/notice/notice223/documents.html?purchaseNoticeNumber=32616576327&noticeGuid=38f2fc7f-c0f9-48d6-b181-e4386a620809">Документы</a>'
    eis = FakeEIS({
        card: card_html,
        tab: '<a href="https://zakupki.gov.ru/223/purchase/public/download/download.html?id=777&amp;notice=1" title="ТЗ.docx">ТЗ</a>',
    })
    page, links, tried = tender_docs.locate_documents(eis, card)
    assert page == tab
    assert links == [("ТЗ.docx", "https://zakupki.gov.ru/223/purchase/public/download/download.html?id=777&notice=1")]
    assert any("файлов: 1" in t for t in tried)
