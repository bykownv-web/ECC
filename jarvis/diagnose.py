"""Диагностика J.A.R.V.I.S. на одной закупке: проходит всю цепочку и пишет подробный отчёт.

Запуск:  .venv\\Scripts\\python.exe diagnose.py 0318100005226000001 [--ai]
--ai  — дополнительно прогнать разбор ТЗ, подбор и расчёт выбранной в настройках моделью (долго на Ollama).

Отчёт: Документы\\Jarvis\\Диагностика\\<номер>\\ (log.txt, сохранённые страницы ЕИС, скачанные файлы)
и архив diag_<номер>.zip рядом — его можно переслать разработчику. Секретов (ключей) в отчёте нет.

Каждая ошибка печатается одной строкой «[FAIL] <шаг>: <причина>», а в конце — полный список ошибок,
так что «ошибок: N» всегда можно сопоставить с конкретными строками.
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
from jarvis_app.procurement import ProcurementClient, SearchParams, is_actual
from jarvis_app.relevance import DEFAULT_MINUS_WORDS, minus_hit, parse_minus_words, tag_matches


def one_line(text: object, limit: int = 400) -> str:
    line = " ".join(str(text or "").split())
    return (line[: limit - 1] + "…") if len(line) > limit else line


def reason_of(exc: BaseException) -> str:
    text = one_line(exc)
    return text if text else f"{exc.__class__.__name__} (без описания)"


class Log:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lines: list[str] = []
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.section = ""

    @property
    def failed(self) -> int:
        return len(self.failures)

    def __call__(self, text: str = "") -> None:
        print(text, flush=True)
        self.lines.append(text)
        try:
            self.path.write_text("\n".join(self.lines), encoding="utf-8")
        except OSError:
            pass

    def step(self, title: str) -> None:
        self.section = title
        self(f"\n=== {title} ===")

    def ok(self, text: str) -> None:
        self(f"[OK]   {one_line(text, 2000)}")

    def warn(self, text: str) -> None:
        line = one_line(text)
        self.warnings.append(f"{self.section}: {line}")
        self(f"[WARN] {line}")

    def fail(self, what: str, reason: str) -> None:
        """Одна строка: что не получилось и почему."""
        line = f"{one_line(what, 120)}: {one_line(reason) or 'причина не указана'}"
        self.failures.append(f"{self.section} → {line}")
        self(f"[FAIL] {line}")

    def detail(self, text: str) -> None:
        """Подробности (трассировка, сырой ответ) — с отступом, после строки [FAIL]."""
        for row in str(text).rstrip().splitlines():
            self(f"       {row}")


def _tags(value) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    return [str(t).strip() for t in value or [] if str(t).strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Диагностика Джарвиса на закупке")
    parser.add_argument("number", help="номер закупки или ссылка на неё")
    parser.add_argument("--ai", action="store_true", help="прогнать ИИ-разбор ТЗ, подбор и расчёт")
    args = parser.parse_args(argv)

    raw = args.number.strip()
    tail = raw.split("regNumber=")[-1] if "regNumber=" in raw else raw
    number = "".join(ch for ch in tail.split("&")[0] if ch.isdigit())
    out = documents_dir() / "Диагностика" / (number or "закупка")
    shutil.rmtree(out, ignore_errors=True)
    (out / "files").mkdir(parents=True, exist_ok=True)
    log = Log(out / "log.txt")
    config = Config()
    proc = config.get("procurement", {}) or {}

    log(f"J.A.R.V.I.S. {__version__} · Python {platform.python_version()} · {platform.platform()}")
    log(f"ИИ: {config.get('ai_provider')} · модель Ollama {config.get('ollama_model')} · облако {config.get('openai_model')} · GigaChat {config.get('gigachat_model')}")
    log(f"Теги: {_tags(proc.get('tags'))} · строгий поиск: {proc.get('strict', True)}")

    try:
        client = ProcurementClient(ca_bundle=proc.get("ca_bundle", ""))
    except Exception as exc:
        log.step("0. Подключение к ЕИС")
        log.fail("настройка HTTPS", reason_of(exc))
        log.detail(traceback.format_exc())
        return finish(log, out, number)
    log(f"Проверка сертификатов: {'свой набор с сертификатами Минцифры' if isinstance(client._verify, str) else 'хранилище Windows'}")

    # 1. поиск
    log.step("1. Поиск закупки в ЕИС по номеру")
    purchase = None
    if len(number) < 11:
        log.fail("номер закупки", f"«{raw}» — не номер (нужно 11 или 19 цифр) и не ссылка с regNumber")
    else:
        try:
            t = time.time()
            found = client.search(SearchParams(query=number, only_active=False))
            log.ok(f"поиск ответил за {time.time() - t:.1f} с, карточек на странице: {len(found)}")
            purchase = next((p for p in found if p.number == number), None)
            if purchase:
                log.ok("закупка найдена: " + json.dumps(purchase.to_dict(), ensure_ascii=False))
                log(f"       актуальна (подача заявок и срок не прошёл): {is_actual(purchase)}")
            else:
                log.fail("поиск", f"в результатах поиска нет закупки № {number} (найдено других: {len(found)})")
        except Exception as exc:
            log.fail("поиск", reason_of(exc))
            log.detail(traceback.format_exc())

    # 2. релевантность
    if purchase:
        log.step("2. Фильтр релевантности (почему закупка показывается или скрывается)")
        try:
            tags = _tags(proc.get("tags"))
            matched = [t for t in tags if tag_matches(purchase.title, t)]
            hit = minus_hit(purchase.title, parse_minus_words(proc.get("minus_words", DEFAULT_MINUS_WORDS)))
            log(f"совпавшие теги: {matched or 'нет'}")
            log(f"минус-слово: {hit or 'нет'}")
            log("итог: " + ("показывается" if matched and not hit else "СКРЫВАЕТСЯ в строгом режиме"))
        except Exception as exc:
            log.fail("фильтр релевантности", reason_of(exc))
            log.detail(traceback.format_exc())

    # 3. карточка и страница документов
    url = purchase.url if purchase else tender_docs.purchase_url_from_input(raw) or ""
    files: list[Path] = []
    if url:
        log.step("3. Карточка и документы закупки")
        log(f"карточка: {url}")
        try:
            card = client.get_text(url)
            (out / "card.html").write_text(card, encoding="utf-8")
            log.ok(f"карточка загружена, {len(card)} символов")
        except Exception as exc:
            log.fail("карточка", reason_of(exc))
        links: list[tuple[str, str]] = []
        try:
            docs_url, links, tried = tender_docs.locate_documents(client, url)
            for line in tried:
                log(f"       попытка: {line}")
            log.ok(f"страница с файлами: {docs_url}")
            log.ok(f"найдено ссылок на файлы: {len(links)}")
            for name, link in links:
                log(f"       • {name}  ←  {link}")
        except Exception as exc:
            log.fail("поиск файлов на странице «Документы»", reason_of(exc))
            log.detail(traceback.format_exc())
        if links:
            errors: list[str] = []
            try:
                t = time.time()
                files = tender_docs.download_documents(client, links, out / "files", errors)
                log.ok(f"скачано файлов: {len(files)} из {len(links)} за {time.time() - t:.1f} с")
                for path in files:
                    log(f"       • {path.name} ({path.stat().st_size:,} байт)".replace(",", " "))
            except Exception as exc:
                log.fail("скачивание файлов", reason_of(exc))
                log.detail(traceback.format_exc())
            for err in errors:
                log.fail("скачивание файла", err)
    else:
        log.step("3. Карточка и документы закупки")
        log.fail("ссылка на закупку", "закупка не найдена в поиске, а по номеру не удалось составить ссылку")

    # 4. текст
    text = ""
    if files:
        log.step("4. Извлечение текста")
        try:
            infos, text = tender_docs.process_files(files, out / "files")
            for f in infos:
                line = f"{f.name}: {f.status} ({f.chars} симв.)"
                if f.kind == "error":
                    log.fail(f.name, f.status.removeprefix("ошибка: "))
                elif f.kind in ("warning", "skipped"):
                    (log.warn if f.kind == "warning" else log)(("[SKIP] " if f.kind == "skipped" else "") + line)
                else:
                    log.ok(line)
            (out / "tz_text.txt").write_text(text, encoding="utf-8")
            if text:
                log.ok(f"текст для ИИ: {len(text)} символов")
                log(f"первые 600 символов:\n{text[:600]}")
            else:
                log.fail("текст для ИИ", "ни из одного файла не удалось извлечь текст (сканы или неподдерживаемые форматы)")
        except Exception as exc:
            log.fail("извлечение текста", reason_of(exc))
            log.detail(traceback.format_exc())

    # 5. ИИ
    if args.ai and text:
        run_ai(log, out, config, text, purchase.to_dict() if purchase else {"number": number, "url": url})
    elif args.ai:
        log.step("5. ИИ: разбор ТЗ, подбор, стоимость")
        log.warn("пропущено: нет текста документов (см. шаги 3–4)")

    return finish(log, out, number)


def run_ai(log: Log, out: Path, config: Config, text: str, purchase: dict) -> None:
    from jarvis_app.analyst import estimate_costs
    from jarvis_app.llm import LLM

    log.step("5. ИИ: разбор ТЗ, подбор, стоимость")
    llm = LLM(config)

    def failed(what: str, exc: BaseException) -> None:
        log.fail(what, reason_of(exc))
        raw = getattr(exc, "raw", "")
        if raw:
            (out / "llm_raw_answer.txt").write_text(raw, encoding="utf-8")
            log.detail(f"сырой ответ модели ({len(raw)} симв.), начало и конец:\n{raw[:800]}\n…\n{raw[-400:]}")
        log.detail(traceback.format_exc())

    try:
        t = time.time()
        analysis = tender_ai.analyze_tz(llm, text, on_progress=log)
        log.ok(f"разбор ТЗ за {time.time() - t:.0f} с, позиций: {len(analysis.get('positions') or [])}")
        for err in analysis.get("errors") or []:
            log.warn(f"разбор ТЗ: {err}")
        (out / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=1), encoding="utf-8")
        for key in ("delivery_terms", "delivery_place", "installation", "payment_terms", "bid_security", "contract_security"):
            log(f"       {key}: {analysis.get(key) or '—'}")
    except Exception as exc:
        failed("разбор ТЗ", exc)
        return
    if analysis.get("positions"):
        try:
            t = time.time()
            matches = tender_ai.match_equipment(llm, analysis, None, on_progress=log)
            log.ok(f"подбор за {time.time() - t:.0f} с: " + "; ".join(f"{m['position']} → {m['brand']} {m['model']}" for m in matches))
        except Exception as exc:
            failed("подбор оборудования", exc)
    else:
        log.warn("подбор пропущен: в ТЗ не найдено позиций оборудования")
    try:
        t = time.time()
        estimate, totals = estimate_costs(llm, purchase, analysis)
        log.ok(f"расчёт за {time.time() - t:.0f} с: себестоимость {totals['cost_min']:,.0f}–{totals['cost_max']:,.0f} ₽, "
               f"маржа {totals.get('margin_pct_min')}…{totals.get('margin_pct_max')}%")
        (out / "estimate.json").write_text(json.dumps({"estimate": estimate, "totals": totals}, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:
        failed("расчёт стоимости", exc)


def finish(log: Log, out: Path, number: str) -> int:
    log.step("Итог")
    if log.failures:
        log(f"ошибок: {log.failed}")
        for i, item in enumerate(log.failures, start=1):
            log(f"  {i}. {item}")
    else:
        log("ошибок нет")
    if log.warnings:
        log(f"предупреждений: {len(log.warnings)}")
        for item in log.warnings:
            log(f"  • {item}")
    try:
        archive = shutil.make_archive(str(out.parent / f"diag_{number or 'закупка'}"), "zip", out)
        log(f"отчёт: {out}\nархив для разработчика: {archive}")
    except OSError as exc:
        log(f"отчёт: {out} (архив не создан: {exc})")
    return 0 if not log.failures else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано.")
        sys.exit(130)
