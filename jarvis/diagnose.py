"""Диагностика J.A.R.V.I.S. на одной закупке: проходит всю цепочку и пишет подробный отчёт.

Запуск:  .venv\\Scripts\\python.exe diagnose.py 0318100005226000001 [--ai]
--ai  — дополнительно прогнать разбор ТЗ, подбор и расчёт выбранной в настройках моделью (долго на Ollama).

Отчёт: Документы\\Jarvis\\Диагностика\\<номер>\\ (log.txt, сохранённые страницы ЕИС, скачанные файлы)
и архив diag_<номер>.zip рядом — его можно переслать разработчику. Секретов (ключей) в отчёте нет.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
import time
import traceback
from pathlib import Path

from jarvis_app import __version__, tender_ai, tender_docs
from jarvis_app.config import Config, documents_dir
from jarvis_app.procurement import ProcurementClient, SearchParams, is_actual, parse_results
from jarvis_app.relevance import DEFAULT_MINUS_WORDS, minus_hit, parse_minus_words, tag_matches


class Log:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lines: list[str] = []
        self.failed = 0

    def __call__(self, text: str = "") -> None:
        print(text, flush=True)
        self.lines.append(text)
        self.path.write_text("\n".join(self.lines), encoding="utf-8")

    def step(self, title: str) -> None:
        self(f"\n=== {title} ===")

    def ok(self, text: str) -> None:
        self(f"[OK]   {text}")

    def fail(self, text: str) -> None:
        self.failed += 1
        self(f"[FAIL] {text}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Диагностика Джарвиса на закупке")
    parser.add_argument("number", help="номер закупки или ссылка на неё")
    parser.add_argument("--ai", action="store_true", help="прогнать ИИ-разбор ТЗ, подбор и расчёт")
    args = parser.parse_args()

    raw = args.number.strip()
    number = "".join(ch for ch in raw.split("regNumber=")[-1] if ch.isdigit())
    out = documents_dir() / "Диагностика" / (number or "закупка")
    shutil.rmtree(out, ignore_errors=True)
    (out / "files").mkdir(parents=True, exist_ok=True)
    log = Log(out / "log.txt")
    config = Config()
    proc = config.get("procurement", {})

    log(f"J.A.R.V.I.S. {__version__} · Python {platform.python_version()} · {platform.platform()}")
    log(f"ИИ: {config.get('ai_provider')} · модель Ollama {config.get('ollama_model')} · облако {config.get('openai_model')} · GigaChat {config.get('gigachat_model')}")
    log(f"Теги: {proc.get('tags')} · строгий поиск: {proc.get('strict', True)}")

    client = ProcurementClient(ca_bundle=proc.get("ca_bundle", ""))
    log(f"Проверка сертификатов: {'свой набор с сертификатами Минцифры' if isinstance(client._verify, str) else 'хранилище Windows'}")

    # 1. поиск
    log.step("1. Поиск закупки в ЕИС по номеру")
    purchase = None
    try:
        t = time.time()
        found = client.search(SearchParams(query=number, only_active=False))
        log.ok(f"поиск ответил за {time.time() - t:.1f} с, карточек на странице: {len(found)}")
        purchase = next((p for p in found if p.number == number), None)
        if purchase:
            log.ok("закупка найдена: " + json.dumps(purchase.to_dict(), ensure_ascii=False))
            log(f"       актуальна (подача заявок и срок не прошёл): {is_actual(purchase)}")
        else:
            log.fail("в результатах поиска нет закупки с этим номером")
    except Exception as exc:
        log.fail(f"поиск: {exc}")
        log(traceback.format_exc())

    # 2. релевантность
    if purchase:
        log.step("2. Фильтр релевантности (почему закупка показывается или скрывается)")
        tags = proc.get("tags") or []
        matched = [t for t in tags if tag_matches(purchase.title, t)]
        hit = minus_hit(purchase.title, parse_minus_words(proc.get("minus_words") or DEFAULT_MINUS_WORDS))
        log(f"совпавшие теги: {matched or 'нет'}")
        log(f"минус-слово: {hit or 'нет'}")
        log("итог: " + ("показывается" if matched and not hit else "СКРЫВАЕТСЯ в строгом режиме"))

    # 3. карточка и страница документов
    url = purchase.url if purchase else tender_docs.purchase_url_from_input(raw) or ""
    files: list[Path] = []
    if url:
        log.step("3. Карточка и документы закупки")
        log(f"карточка: {url}")
        card = ""
        try:
            card = client.get_text(url)
            (out / "card.html").write_text(card, encoding="utf-8")
            log.ok(f"карточка загружена, {len(card)} символов")
            parsed = parse_results(card)
            log(f"       (служебно: карточек поиска на странице {len(parsed)})")
        except Exception as exc:
            log.fail(f"карточка: {exc}")
        try:
            docs_url, links, tried = tender_docs.locate_documents(client, url)
            for line in tried:
                log(f"       попытка: {line}")
            log.ok(f"страница с файлами: {docs_url}")
            log.ok(f"найдено файлов: {len(links)}")
            for name, link in links:
                log(f"       • {name}  ←  {link}")
            t = time.time()
            files = tender_docs.fetch_eis_documents(client, url, out / "files")
            log.ok(f"скачано файлов: {len(files)} за {time.time() - t:.1f} с")
        except Exception as exc:
            log.fail(f"документы: {exc}")
            log(traceback.format_exc())

    # 4. текст
    text = ""
    if files:
        log.step("4. Извлечение текста")
        infos, text = tender_docs.process_files(files, out / "files")
        for f in infos:
            (log.ok if "извлечён" in f.status or "архив" in f.status else log.fail)(f"{f.name}: {f.status} ({f.chars} симв.)")
        (out / "tz_text.txt").write_text(text, encoding="utf-8")
        log(f"текст для ИИ: {len(text)} символов (первые 600):\n{text[:600]}")

    # 5. ИИ
    if args.ai and text:
        from jarvis_app.analyst import estimate_costs
        from jarvis_app.llm import LLM

        log.step("5. ИИ: разбор ТЗ, подбор, стоимость")
        llm = LLM(config)
        try:
            t = time.time()
            analysis = tender_ai.analyze_tz(llm, text, on_progress=log)
            log.ok(f"разбор ТЗ за {time.time() - t:.0f} с, позиций: {len(analysis.get('positions') or [])}")
            (out / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=1), encoding="utf-8")
            for key in ("delivery_terms", "delivery_place", "installation", "payment_terms", "bid_security", "contract_security"):
                log(f"       {key}: {analysis.get(key) or '—'}")
            t = time.time()
            matches = tender_ai.match_equipment(llm, analysis, None, on_progress=log) if analysis.get("positions") else []
            log.ok(f"подбор за {time.time() - t:.0f} с: " + "; ".join(f"{m['position']} → {m['brand']} {m['model']}" for m in matches))
            t = time.time()
            p = purchase.to_dict() if purchase else {"number": number, "url": url}
            estimate, totals = estimate_costs(llm, p, analysis)
            log.ok(f"расчёт за {time.time() - t:.0f} с: себестоимость {totals['cost_min']:,.0f}–{totals['cost_max']:,.0f} ₽, маржа {totals.get('margin_pct_min')}…{totals.get('margin_pct_max')}%")
            (out / "estimate.json").write_text(json.dumps({"estimate": estimate, "totals": totals}, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:
            log.fail(f"ИИ: {exc}")
            raw = getattr(exc, "raw", "")
            if raw:
                (out / "llm_raw_answer.txt").write_text(raw, encoding="utf-8")
                log(f"       сырой ответ модели ({len(raw)} симв.), начало и конец:\n{raw[:800]}\n…\n{raw[-400:]}")
            log(traceback.format_exc())

    log.step("Итог")
    log("ошибок нет" if not log.failed else f"ошибок: {log.failed}")
    archive = shutil.make_archive(str(out.parent / f"diag_{number}"), "zip", out)
    log(f"отчёт: {out}\nархив для разработчика: {archive}")
    return 0 if not log.failed else 1


if __name__ == "__main__":
    sys.exit(main())
