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

    with pytest.raises(ValueError, match="docx"):
        tender_docs.extract_text(tmp_path / "old.doc")


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
