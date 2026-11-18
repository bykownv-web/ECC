"""Документы закупки: скачивание с ЕИС и извлечение текста из .docx, .pdf, .xlsx, .txt, .rtf и .zip."""

from __future__ import annotations

import re
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .procurement import BASE_URL, ProcurementClient, ProcurementError

SUPPORTED = {".docx", ".doc", ".pdf", ".xlsx", ".xls", ".xlsm", ".txt", ".csv", ".rtf", ".zip"}
MAX_FILE_BYTES = 40 * 1024 * 1024
MAX_FILES = 20
MAX_TEXT_CHARS = 400_000
ANALYSIS_CHARS = 40_000  # сколько текста отдаём ИИ: больше — дольше и не точнее
CONTRACT_CHARS = 9_000

_PRIORITY = [
    (0, ("техническ", "тз", "описание объекта", "спецификац", "требования к", "характеристик")),
    (1, ("извещ", "обоснован", "нмцк", "информационн", "документац", "инструкц")),
    (3, ("проект контракт", "проект договор", "контракт", "договор")),
]
_CONTRACT_KEYS = re.compile(
    r"(срок|поставк|оплат|приемк|приёмк|гарант|неустойк|штраф|пен[яи]|монтаж|пусконаладк|обучени|место|адрес|обеспечени)",
    re.I,
)


def file_priority(name: str) -> int:
    low = name.lower().replace("_", " ")
    for prio, keys in _PRIORITY:
        if any(k in low for k in keys):
            return prio
    return 2


def contract_extract(text: str, limit: int = CONTRACT_CHARS) -> str:
    """Из длинного проекта контракта берём только абзацы про сроки, оплату, приёмку, гарантию, штрафы, монтаж."""
    picked: list[str] = []
    total = 0
    for para in text.split("\n"):
        if _CONTRACT_KEYS.search(para):
            picked.append(para.strip())
            total += len(para)
            if total > limit:
                break
    return "\n".join(picked)[:limit]


def build_analysis_text(parts: list[tuple[str, str]], limit: int = ANALYSIS_CHARS) -> str:
    """ТЗ и спецификации — первыми, затем извещение, потом прочее; проект контракта — выжимкой."""
    ordered = sorted(parts, key=lambda p: file_priority(p[0]))
    chunks: list[str] = []
    total = 0
    for name, text in ordered:
        if file_priority(name) == 3:
            text = contract_extract(text)
        if not text.strip():
            continue
        room = limit - total
        if room <= 500:
            break
        piece = text[:room]
        chunks.append(f"===== Файл: {name} =====\n{piece}")
        total += len(piece)
    return "\n\n".join(chunks)


@dataclass
class TenderFile:
    name: str
    path: str
    chars: int = 0
    status: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def safe_name(name: str, fallback: str = "document") -> str:
    name = unquote(name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return (name or fallback)[:150]


# ---------- извлечение текста ----------
def _docx_text(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells: list[str] = []
            for cell in row.cells:
                value = " ".join(cell.text.split())
                if value and (not cells or cells[-1] != value):  # объединённые ячейки повторяются
                    cells.append(value)
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _xlsx_text(path: Path) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    lines: list[str] = []
    for ws in wb.worksheets:
        lines.append(f"[Лист {ws.title}]")
        for row in ws.iter_rows(values_only=True):
            values = [str(v).strip() for v in row if v is not None and str(v).strip()]
            if values:
                lines.append(" | ".join(values))
    wb.close()
    return "\n".join(lines)


def _rtf_text(path: Path) -> str:
    raw = path.read_bytes().decode("cp1251", errors="ignore")

    def hexchar(m: re.Match) -> str:
        return bytes([int(m.group(1), 16)]).decode("cp1251", errors="ignore")

    raw = re.sub(r"\\'([0-9a-fA-F]{2})", hexchar, raw)
    raw = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), raw)
    raw = re.sub(r"\\par[d]?\b", "\n", raw)
    raw = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", raw)
    raw = raw.replace("{", "").replace("}", "")
    return re.sub(r"\n\s*\n+", "\n", raw)


def _plain_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


_OFFICE_PS = r"""
$ErrorActionPreference = 'Stop'
$src = $env:JARVIS_SRC; $dst = $env:JARVIS_DST
if ($src.ToLower().EndsWith('.doc')) {
  $app = New-Object -ComObject Word.Application; $app.Visible = $false; $app.DisplayAlerts = 0
  try { $d = $app.Documents.Open($src, $false, $true); $d.SaveAs2($dst, 16); $d.Close($false) } finally { $app.Quit() }
} else {
  $app = New-Object -ComObject Excel.Application; $app.Visible = $false; $app.DisplayAlerts = $false
  try { $w = $app.Workbooks.Open($src, 0, $true); $w.SaveAs($dst, 51); $w.Close($false) } finally { $app.Quit() }
}
"""


def convert_with_office(path: Path) -> Path | None:
    """.doc → .docx и .xls → .xlsx через установленный Microsoft Office (Windows). None — если не вышло."""
    import os
    import platform
    import subprocess

    if platform.system() != "Windows":
        return None
    target = path.with_suffix(".docx" if path.suffix.lower() == ".doc" else ".xlsx")
    if target.exists():
        return target
    env = dict(os.environ, JARVIS_SRC=str(path.resolve()), JARVIS_DST=str(target.resolve()))
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _OFFICE_PS], env=env,
                       capture_output=True, timeout=120, creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError):
        return None
    return target if target.exists() else None


def _legacy_doc_text(path: Path) -> str:
    """Запасной способ для .doc без Word: вытаскиваем текст из бинарника (UTF-16 и cp1251)."""
    data = path.read_bytes()
    runs = re.findall(rb"(?:[\x20-\x7e\x0d\x0a]\x00|[\x10-\x4f]\x04){6,}", data)
    text = "\n".join(r.decode("utf-16-le", errors="ignore") for r in runs)
    if len(re.findall(r"[А-Яа-я]", text)) < 50:
        runs = re.findall(rb"[\xc0-\xff\x20-\x7e\xa8\xb8\x0d\x0a]{12,}", data)
        text = "\n".join(r.decode("cp1251", errors="ignore") for r in runs)
    lines = [ln.strip() for ln in text.replace("\r", "\n").split("\n") if len(re.findall(r"[А-Яа-яA-Za-z]", ln)) >= 4]
    return "\n".join(lines)


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _docx_text(path)
    if suffix == ".pdf":
        return _pdf_text(path)
    if suffix in (".xlsx", ".xlsm"):
        return _xlsx_text(path)
    if suffix == ".rtf":
        return _rtf_text(path)
    if suffix in (".txt", ".csv"):
        return _plain_text(path)
    if suffix in (".doc", ".xls"):
        converted = convert_with_office(path)
        if converted is not None:
            return extract_text(converted)
        if suffix == ".doc":
            text = _legacy_doc_text(path)
            if text:
                return text
            raise ValueError("старый формат .doc: откройте файл в Word и сохраните как .docx")
        raise ValueError("старый формат .xls: откройте в Excel и сохраните как .xlsx")
    raise ValueError(f"формат {suffix or 'без расширения'} не поддерживается")


def unpack_zip(path: Path, dest: Path) -> list[Path]:
    """Распаковывает архив, защищаясь от путей вида ../ и слишком больших файлов."""
    out: list[Path] = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist()[:MAX_FILES]:
            if info.is_dir() or info.file_size > MAX_FILE_BYTES:
                continue
            raw_name = info.filename
            if not (info.flag_bits & 0x800):  # имя не в UTF-8 — обычно cp866 из Windows-архиваторов
                try:
                    raw_name = raw_name.encode("cp437").decode("cp866")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            target = dest / safe_name(Path(raw_name).name)
            with zf.open(info) as src:
                target.write_bytes(src.read(MAX_FILE_BYTES + 1)[:MAX_FILE_BYTES])
            out.append(target)
    return out


def process_files(paths: list[Path], work_dir: Path) -> tuple[list[TenderFile], str]:
    """Извлекает текст из файлов. Возвращает список файлов со статусом и общий текст."""
    files: list[TenderFile] = []
    texts: list[tuple[str, str]] = []
    queue = list(paths)
    while queue and len(files) < MAX_FILES * 2:
        path = queue.pop(0)
        item = TenderFile(name=path.name, path=str(path))
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("файл больше 40 МБ")
            if path.suffix.lower() == ".zip":
                inner_dir = work_dir / safe_name(path.stem + "_zip")
                inner_dir.mkdir(parents=True, exist_ok=True)
                inner = unpack_zip(path, inner_dir)
                queue.extend(inner)
                item.status = f"архив: {len(inner)} файлов"
            else:
                text = extract_text(path).strip()
                item.chars = len(text)
                if text:
                    texts.append((path.name, text))
                    item.status = "текст извлечён"
                else:
                    item.status = "текст не найден (возможно, скан — нужен файл с текстом)"
        except (ValueError, OSError, zipfile.BadZipFile) as exc:
            item.status = f"ошибка: {exc}"
        except Exception as exc:  # повреждённый файл не должен ронять разбор остальных
            item.status = f"ошибка чтения: {exc.__class__.__name__}"
        files.append(item)
    return files, build_analysis_text(texts)


# ---------- ЕИС ----------
def documents_page_url(purchase_url: str) -> str:
    """Карточка закупки -> страница «Документы» той же закупки."""
    parsed = urlparse(purchase_url)
    path = re.sub(r"/[a-z\-]+\.html$", "/documents.html", parsed.path)
    if path == parsed.path and not path.endswith("documents.html"):
        path = parsed.path.rsplit("/", 1)[0] + "/documents.html"
    return parsed._replace(path=path).geturl()


def parse_document_links(html: str, page_url: str) -> list[tuple[str, str]]:
    """Ссылки на файлы закупки: (имя файла, адрес)."""
    soup = BeautifulSoup(html, "html.parser")
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        href = link["href"]
        # Ссылки на просмотр электронной подписи (signview/listModal) — не файлы.
        if re.search(r"(signview|listModal|/sign/|printForm)", href, re.I):
            continue
        if not re.search(r"(filestore|/download|/file\.html)", href, re.I):
            continue
        url = urljoin(page_url, href)
        host = urlparse(url).netloc
        if not host.endswith("zakupki.gov.ru") or url in seen:
            continue
        seen.add(url)
        title = (link.get("title") or link.get_text(" ", strip=True) or "").strip()
        found.append((safe_name(title, f"document_{len(found) + 1}"), url))
    return found[:MAX_FILES]


def fetch_eis_documents(client: ProcurementClient, purchase_url: str, dest: Path) -> list[Path]:
    page_url = documents_page_url(purchase_url)
    html = client.get_text(page_url)
    links = parse_document_links(html, page_url)
    if not links:
        raise ProcurementError(
            "На странице документов закупки не нашлось файлов. Скачайте ТЗ с сайта вручную "
            "и добавьте кнопкой «Выбрать файлы»."
        )
    dest.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for name, url in links:
        data, filename = client.get_file(url)
        final = safe_name(filename or name)
        if not Path(final).suffix and Path(name).suffix:
            final = name
        target = dest / final
        target.write_bytes(data[:MAX_FILE_BYTES])
        saved.append(target)
    return saved


def purchase_url_from_input(value: str) -> str | None:
    """Номер закупки или ссылка -> ссылка на карточку."""
    value = (value or "").strip()
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        if parsed.netloc.endswith("zakupki.gov.ru"):
            return parsed._replace(scheme="https").geturl()
        return None
    number = re.sub(r"\D", "", value)
    if len(number) == 19:
        return f"{BASE_URL}/epz/order/notice/ea20/view/common-info.html?regNumber={number}"
    if len(number) == 11:
        return f"{BASE_URL}/epz/order/notice/notice223/common-info.html?regNumber={number}"
    return None
