"""Документы закупки: скачивание с ЕИС и извлечение текста из .docx, .doc, .pdf, .xlsx, .xls, .txt, .rtf и .zip."""

from __future__ import annotations

import hashlib
import re
import struct
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .procurement import BASE_URL, ProcurementClient, ProcurementError, blocked_message, blocked_reason

SUPPORTED = {".docx", ".doc", ".pdf", ".xlsx", ".xls", ".xlsm", ".txt", ".csv", ".rtf", ".zip"}
SIGNATURES = {".sig", ".p7s", ".sgn", ".p7m", ".sign"}
IMAGES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif"}
UNSUPPORTED_HINT = {
    ".rar": "архив RAR не поддерживается — распакуйте вручную и добавьте файлы",
    ".7z": "архив 7z не поддерживается — распакуйте вручную и добавьте файлы",
    ".odt": "формат .odt: сохраните файл как .docx",
    ".ods": "формат .ods: сохраните файл как .xlsx",
    ".html": "веб-страница вместо документа",
    ".htm": "веб-страница вместо документа",
}
KNOWN_SUFFIXES = SUPPORTED | SIGNATURES | IMAGES | set(UNSUPPORTED_HINT) | {".xml", ".pptx", ".ppt"}
MAX_FILE_BYTES = 40 * 1024 * 1024
MAX_FILES = 20
MAX_TEXT_CHARS = 400_000
ANALYSIS_CHARS = 40_000  # сколько текста отдаём ИИ: больше — дольше и не точнее
CONTRACT_CHARS = 9_000

_TZ_RE = re.compile(r"(^|[^а-яёa-z])тз([^а-яёa-z]|$)")
_PRIORITY = [
    (0, ("техническ", "описание объекта", "спецификац", "характеристик")),
    (1, ("требования к заявк", "требования к содержанию", "требования к составу", "инструкц", "извещ", "обоснован",
         "нмцк", "информационн", "документац")),
    (0, ("требования к",)),
    (3, ("проект контракт", "проект договор", "контракт", "договор")),
]
_CONTRACT_KEYS = re.compile(
    r"(срок|поставк|оплат|приемк|приёмк|гарант|неустойк|штраф|пен[яи]|монтаж|пусконаладк|обучени|место|адрес|обеспечени)",
    re.I,
)


def file_priority(name: str) -> int:
    low = name.lower().replace("_", " ")
    if _TZ_RE.search(low) or any(k in low for k in _PRIORITY[0][1]):
        return 0
    for prio, keys in _PRIORITY[1:]:
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


_FORMAT_PREF = {".docx": 0, ".doc": 1, ".rtf": 2, ".xlsx": 3, ".xlsm": 3, ".xls": 4, ".txt": 5, ".csv": 5, ".pdf": 6}


def _doc_key(name: str) -> str:
    """«Приложение № 3 НМЦК (2).pdf» и «Приложение № 3 НМЦК.xlsx» — один и тот же документ в разных форматах."""
    stem = Path(name).stem if known_suffix(name) else name
    return re.sub(r"\s*\(\d+\)$", "", " ".join(stem.lower().split()))


def build_analysis_text(parts: list[tuple[str, str]], limit: int = ANALYSIS_CHARS) -> str:
    """ТЗ и спецификации — первыми, затем извещение, потом прочее; проект контракта — выжимкой.

    Один документ в нескольких форматах (НМЦК.xlsx и НМЦК.pdf) берётся один раз — в лучшем для разбора формате.
    """
    best: dict[str, tuple[str, str]] = {}
    order: list[str] = []
    for name, text in parts:
        key = _doc_key(name)
        if key not in best:
            order.append(key)
            best[key] = (name, text)
        elif _FORMAT_PREF.get(known_suffix(name), 9) < _FORMAT_PREF.get(known_suffix(best[key][0]), 9) and text.strip():
            best[key] = (name, text)
    unique = [best[k] for k in order]
    ordered = sorted(unique, key=lambda p: file_priority(p[0]))
    chunks: list[str] = []
    seen_texts: set[str] = set()
    total = 0
    for name, text in ordered:
        if file_priority(name) == 3:
            text = contract_extract(text)
        if not text.strip():
            continue
        digest = hashlib.sha1(" ".join(text.split()).encode("utf-8")).hexdigest()
        if digest in seen_texts:
            continue
        seen_texts.add(digest)
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
    kind: str = "ok"  # ok | skipped (подпись, дубликат) | warning (нет текста) | error

    def to_dict(self) -> dict:
        return asdict(self)


def known_suffix(name: str) -> str:
    """Расширение, если оно настоящее (.docx, .pdf…). У «Договор от 01.02.2026» расширения нет."""
    suffix = Path(name or "").suffix.lower()
    return suffix if suffix in KNOWN_SUFFIXES else ""


def safe_name(name: str, fallback: str = "document", limit: int = 150) -> str:
    """Имя файла без запрещённых в Windows символов; при обрезке длинного имени расширение сохраняется."""
    name = unquote(name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = " ".join(name.split()).strip(" .")
    name = name or fallback
    if len(name) > limit:
        suffix = known_suffix(name)
        stem = name[: len(name) - len(suffix)] if suffix else name
        name = stem[: limit - len(suffix)].rstrip(" ._") + suffix
    return name


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
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ValueError("PDF защищён паролем") from exc
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _xlsx_text(path: Path) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    lines: list[str] = []
    try:
        for ws in wb.worksheets:
            lines.append(f"[Лист {ws.title}]")
            for row in ws.iter_rows(values_only=True):
                values = [str(v).strip() for v in row if v is not None and str(v).strip()]
                if values:
                    lines.append(" | ".join(values))
    finally:
        wb.close()
    return "\n".join(lines)


_RTF_TOKEN = re.compile(r"\\([a-z]{1,32})(-?\d{1,10})?[ ]?|\\'([0-9a-f]{2})|\\([^a-z])|([{}])|[\r\n]+|(.)", re.I | re.S)
_RTF_SKIP = {
    "fonttbl", "colortbl", "stylesheet", "info", "listtable", "listoverridetable", "pict", "object", "rsidtbl",
    "generator", "xmlnstbl", "mmathpr", "themedata", "colorschememapping", "latentstyles", "datastore", "header",
    "footer", "headerl", "headerr", "headerf", "footerl", "footerr", "footerf", "fldinst", "pntext", "pntxta",
    "pntxtb", "listtext", "revtbl", "filetbl", "operator", "author", "title", "company", "bkmkstart", "bkmkend",
}
_RTF_CHARS = {"par": "\n", "line": "\n", "sect": "\n", "page": "\n", "row": "\n", "cell": " | ", "tab": "\t",
              "emdash": "—", "endash": "–", "lquote": "‘", "rquote": "’", "ldblquote": "«", "rdblquote": "»",
              "bullet": "•", "emspace": " ", "enspace": " ", "qmspace": " "}


def rtf_to_text(raw: str) -> str:
    """Текст из RTF: пропускает служебные группы (шрифты, стили, списки, картинки), понимает \\'hh и \\uN."""
    codepage = "cp1251"
    m = re.search(r"\\ansicpg(\d+)", raw[:2000])
    if m and m.group(1) in ("1250", "1251", "1252", "866"):
        codepage = "cp" + m.group(1)
    stack: list[tuple[int, bool]] = []
    ignorable, ucskip, curskip = False, 1, 0
    out: list[str] = []
    pending: bytearray = bytearray()

    def flush() -> None:
        if pending:
            out.append(pending.decode(codepage, errors="ignore"))
            pending.clear()

    for match in _RTF_TOKEN.finditer(raw):
        word, arg, hexcode, char, brace, tchar = match.groups()
        if hexcode is None:
            flush()
        if brace:
            curskip = 0
            if brace == "{":
                stack.append((ucskip, ignorable))
            elif stack:
                ucskip, ignorable = stack.pop()
        elif char:
            curskip = 0
            if char == "~":
                if not ignorable:
                    out.append(" ")
            elif char in "{}\\":
                if not ignorable:
                    out.append(char)
            elif char == "*":
                ignorable = True
            elif char in "-_":
                if not ignorable and char == "_":
                    out.append("-")
        elif word:
            curskip = 0
            low = word.lower()
            if low in _RTF_SKIP:
                ignorable = True
            elif ignorable:
                pass
            elif low in _RTF_CHARS:
                out.append(_RTF_CHARS[low])
            elif low == "uc":
                ucskip = int(arg or 1)
            elif low == "u":
                code = int(arg or 0)
                out.append(chr(code + 65536 if code < 0 else code))
                curskip = ucskip
        elif hexcode:
            if curskip > 0:
                curskip -= 1
            elif not ignorable:
                pending.append(int(hexcode, 16))
        elif tchar:
            if curskip > 0:
                curskip -= 1
            elif not ignorable:
                out.append(tchar)
    flush()
    lines = [" ".join(line.split()).strip(" |") for line in "".join(out).split("\n")]
    return "\n".join(line for line in lines if line)


def _rtf_text(path: Path) -> str:
    return rtf_to_text(path.read_bytes().decode("latin-1"))


def _plain_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


# ---------- старые форматы Word/Excel (OLE) ----------
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_END = 0xFFFFFFFA  # номера секторов от этого значения и выше — служебные


def ole_streams(data: bytes) -> dict[str, bytes]:
    """Минимальный разбор контейнера OLE (Compound File): имя потока → содержимое."""
    if data[:8] != OLE_MAGIC:
        raise ValueError("не OLE-файл")

    def u16(buf: bytes, off: int) -> int:
        return struct.unpack_from("<H", buf, off)[0]

    def u32(buf: bytes, off: int) -> int:
        return struct.unpack_from("<I", buf, off)[0]

    ssz = 1 << u16(data, 0x1E)
    mssz = 1 << u16(data, 0x20)
    if ssz not in (512, 4096) or mssz != 64:
        raise ValueError("повреждённый OLE-файл")
    n_fat, dir_start, cutoff = u32(data, 0x2C), u32(data, 0x30), u32(data, 0x38)
    minifat_start, difat_start, n_difat = u32(data, 0x3C), u32(data, 0x44), u32(data, 0x48)
    per = ssz // 4

    def sector(n: int) -> bytes:
        off = (n + 1) * ssz
        return data[off:off + ssz]

    difat = list(struct.unpack_from("<109I", data, 0x4C))
    nxt, guard = difat_start, 0
    while nxt < _END and guard <= n_difat and guard < 10000:
        sec = sector(nxt)
        if len(sec) < ssz:
            break
        vals = struct.unpack(f"<{per}I", sec)
        difat.extend(vals[:-1])
        nxt, guard = vals[-1], guard + 1
    fat: list[int] = []
    for fs in [x for x in difat if x < _END][:n_fat]:
        sec = sector(fs)
        if len(sec) == ssz:
            fat.extend(struct.unpack(f"<{per}I", sec))

    def chain(start: int, table: list[int]) -> list[int]:
        out: list[int] = []
        seen: set[int] = set()
        while start < _END and start < len(table) and start not in seen:
            seen.add(start)
            out.append(start)
            start = table[start]
        return out

    def read(start: int, size: int | None = None) -> bytes:
        blob = b"".join(sector(x) for x in chain(start, fat))
        return blob if size is None else blob[:size]

    directory = read(dir_start)
    entries = []
    for i in range(len(directory) // 128):
        e = directory[i * 128:(i + 1) * 128]
        name_len = min(u16(e, 0x40), 64)
        name = e[:max(0, name_len - 2)].decode("utf-16-le", errors="ignore")
        entries.append((name, e[0x42], u32(e, 0x74), u32(e, 0x78)))
    if not entries:
        raise ValueError("повреждённый OLE-файл")
    ministream = read(entries[0][2], entries[0][3])
    minifat_raw = read(minifat_start) if minifat_start < _END else b""
    minifat = list(struct.unpack(f"<{len(minifat_raw) // 4}I", minifat_raw[: len(minifat_raw) // 4 * 4]))
    streams: dict[str, bytes] = {}
    for name, kind, start, size in entries[1:]:
        if kind != 2 or not name:
            continue
        if size < cutoff:
            blob = b"".join(ministream[x * mssz:(x + 1) * mssz] for x in chain(start, minifat))[:size]
        else:
            blob = read(start, size)
        streams.setdefault(name, blob)
    return streams


def _clean_word_text(text: str) -> str:
    """Служебные символы Word: поля, ячейки таблиц, разрывы, объекты."""
    out: list[str] = []
    stack: list[bool] = []  # для каждого открытого поля: показывать ли символы (после разделителя \x14 — да)
    for ch in text:
        if ch == "\x13":
            stack.append(False)
            continue
        if ch == "\x14":
            if stack:
                stack[-1] = True
            continue
        if ch == "\x15":
            if stack:
                stack.pop()
            continue
        if stack and not stack[-1]:
            continue  # код поля (HYPERLINK, PAGE …) — не текст
        out.append(ch)
    text = "".join(out)
    text = text.replace("\x07\x07", "\n").replace("\x07", " | ")
    text = re.sub(r"[\r\x0b\x0c]", "\n", text)
    text = text.replace("\x1e", "-").replace("\x1f", "").replace("\xa0", " ")
    text = re.sub(r"[\x00-\x08\x0e-\x1d]", "", text)
    lines = [" ".join(line.split()).strip(" |") for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def word97_text(data: bytes) -> str:
    """Текст из .doc (Word 97–2003) по таблице фрагментов (piece table) — без Word и сторонних библиотек."""
    streams = ole_streams(data)
    wd = streams.get("WordDocument")
    if not wd or len(wd) < 0x200:
        raise ValueError("в файле нет документа Word")
    ident, nfib, flags = struct.unpack_from("<HHxxxxxxH", wd, 0)
    if ident != 0xA5EC:
        raise ValueError("неизвестный формат Word")
    if flags & 0x0100:
        raise ValueError("файл .doc защищён паролем: откройте его в Word и сохраните без пароля")
    if nfib < 0x00C1:
        raise ValueError("очень старая версия Word (до 97)")
    table = streams.get("1Table" if flags & 0x0200 else "0Table") or b""
    pos = 0x20
    csw = struct.unpack_from("<H", wd, pos)[0]
    pos += 2 + csw * 2
    cslw = struct.unpack_from("<H", wd, pos)[0]
    pos += 2 + cslw * 4
    pos += 2  # cbRgFcLcb
    fc_clx, lcb_clx = struct.unpack_from("<II", wd, pos + 33 * 8)
    clx = table[fc_clx:fc_clx + lcb_clx]
    i = 0
    while i < len(clx) and clx[i] == 0x01:  # Prc — форматирование, пропускаем
        i += 3 + struct.unpack_from("<h", clx, i + 1)[0]
    if i >= len(clx) or clx[i] != 0x02:
        raise ValueError("не найдена таблица текста Word")
    lcb = struct.unpack_from("<I", clx, i + 1)[0]
    plc = clx[i + 5:i + 5 + lcb]
    n = (lcb - 4) // 12
    if n <= 0:
        return ""
    cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
    pieces: list[str] = []
    for k in range(n):
        fc = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * k + 2)[0]
        length = max(0, cps[k + 1] - cps[k])
        if fc & 0x40000000:  # «сжатый» текст: 1 байт на символ (cp1252)
            start = (fc & 0x3FFFFFFF) // 2
            pieces.append(wd[start:start + length].decode("cp1252", errors="replace"))
        else:
            pieces.append(wd[fc:fc + 2 * length].decode("utf-16-le", errors="replace"))
    return _clean_word_text("".join(pieces))


_U16_RUN = re.compile(
    rb"(?:[\x09\x0a\x0d\x20-\x7e\xa0\xab\xb0\xb1\xbb]\x00|[\x01-\x5f]\x04|[\x13\x14\x18-\x1e\x22\x26\x30\x39\x3a]\x20"
    rb"|\x16\x21|[\x64\x65]\x22){4,}"
)


def _legacy_binary_text(data: bytes) -> str:
    """Запасной способ для старых .doc/.xls: вытаскиваем куски текста из бинарника (UTF-16 и cp1251)."""
    try:  # по возможности берём только поток с текстом, без таблиц стилей и шрифтов
        streams = ole_streams(data)
        data = streams.get("WordDocument") or streams.get("Workbook") or streams.get("Book") or data
    except (ValueError, struct.error):
        pass
    utf16 = "\n".join(r.decode("utf-16-le", errors="ignore") for r in _U16_RUN.findall(data))
    runs = re.findall(rb"[\xc0-\xff\xa8\xb8\xab\xbb\xb9\x96\x97\x20-\x7e\x0d\x0a\x09]{12,}", data)
    cp1251 = "\n".join(r.decode("cp1251", errors="ignore") for r in runs)
    text = utf16 if len(re.findall(r"[А-Яа-яЁё]", utf16)) >= len(re.findall(r"[А-Яа-яЁё]", cp1251)) else cp1251
    lines = []
    for ln in text.replace("\r", "\n").split("\n"):
        ln = " ".join(ln.split())
        letters = re.findall(r"[А-Яа-яЁёA-Za-z]", ln)
        if len(letters) < 3 or len(set(ln)) < 4:  # «яяяяяя» из служебных секторов — не текст
            continue
        lines.append(ln)
    return "\n".join(lines)


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


def _soffice_path() -> str | None:
    import shutil

    found = shutil.which("soffice") or shutil.which("soffice.exe")
    if found:
        return found
    for base in (r"C:\Program Files\LibreOffice\program", r"C:\Program Files (x86)\LibreOffice\program"):
        candidate = Path(base) / "soffice.exe"
        if candidate.exists():
            return str(candidate)
    return None


def convert_with_office(path: Path) -> Path | None:
    """.doc → .docx и .xls → .xlsx через Microsoft Office или LibreOffice (Windows). None — если не вышло.

    Результат кладётся в подпапку _converted, чтобы при следующем разборе он не попал в список файлов дважды
    и не перезаписал настоящий .docx с тем же именем.
    """
    import os
    import platform
    import subprocess

    if platform.system() != "Windows":
        return None
    new_suffix = ".docx" if path.suffix.lower() == ".doc" else ".xlsx"
    out_dir = path.parent / "_converted"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    target = out_dir / (path.stem + new_suffix)
    try:
        if target.exists() and target.stat().st_mtime >= path.stat().st_mtime:
            return target
    except OSError:
        pass
    env = dict(os.environ, JARVIS_SRC=str(path.resolve()), JARVIS_DST=str(target.resolve()))
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _OFFICE_PS], env=env,
                       capture_output=True, timeout=120, creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    if target.exists() and target.stat().st_size > 0:
        return target
    soffice = _soffice_path()
    if soffice:
        try:
            subprocess.run([soffice, "--headless", "--convert-to", new_suffix.lstrip("."), "--outdir", str(out_dir), str(path)],
                           capture_output=True, timeout=120, creationflags=0x08000000)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
    return target if target.exists() and target.stat().st_size > 0 else None


def _legacy_doc_text(path: Path) -> str:
    """.doc без Word: сначала точный разбор Word 97–2003, при неудаче — извлечение кусков текста из бинарника."""
    data = path.read_bytes()
    if data[:5] == b"{\\rtf":
        return _rtf_text(path)
    try:
        text = word97_text(data)
        if len(re.findall(r"[А-Яа-яЁёA-Za-z]", text)) >= 3:
            return text
    except ValueError as exc:
        if "паролем" in str(exc):
            raise
    except (struct.error, IndexError, OverflowError, MemoryError):
        pass
    return _legacy_binary_text(data)


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
    if suffix == ".doc":
        text = _legacy_doc_text(path)
        if text.strip():
            return text
        converted = _safe_convert(path)
        if converted is not None:
            return extract_text(converted)
        raise ValueError("старый формат .doc не прочитался: откройте файл в Word и сохраните как .docx")
    if suffix == ".xls":
        converted = _safe_convert(path)
        if converted is not None:
            return extract_text(converted)
        text = _legacy_binary_text(path.read_bytes())
        if len(re.findall(r"[А-Яа-яЁё]", text)) >= 20:
            return "[Текст из .xls извлечён упрощённо: числа могут потеряться]\n" + text
        raise ValueError("старый формат .xls: откройте в Excel и сохраните как .xlsx")
    raise ValueError(f"формат {suffix or 'без расширения'} не поддерживается")


def _safe_convert(path: Path) -> Path | None:
    """Конвертация через Office никогда не роняет разбор: любая ошибка → None."""
    try:
        converted = convert_with_office(path)
    except Exception:
        return None
    if converted is None:
        return None
    try:  # проверяем, что результат открывается
        if converted.suffix.lower() == ".docx":
            _docx_text(converted)
        return converted
    except Exception:
        return None


# ---------- определение формата по содержимому ----------
def sniff_suffix(data: bytes) -> str:
    """Настоящий формат файла по первым байтам: '.pdf', '.docx', '.xlsx', '.zip', '.doc', '.xls', '.rtf', '.html'…"""
    head = (data or b"")[:8]
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"{\\rtf"):
        return ".rtf"
    if head.startswith(b"PK\x03\x04"):
        import io

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
        except (zipfile.BadZipFile, OSError, ValueError):
            return ".zip"
        if any(n.startswith("word/") for n in names):
            return ".docx"
        if any(n.startswith("xl/") for n in names):
            return ".xlsx"
        if any(n.startswith("ppt/") for n in names):
            return ".pptx"
        return ".zip"
    if head == OLE_MAGIC:
        try:
            streams = ole_streams(data)
        except (ValueError, struct.error):
            return ".doc"
        if "Workbook" in streams or "Book" in streams:
            return ".xls"
        return ".doc"
    if head.startswith(b"Rar!"):
        return ".rar"
    if head.startswith(b"7z\xbc\xaf"):
        return ".7z"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tif"
    from .procurement import looks_like_html

    if looks_like_html(data):
        return ".html"
    return ""


_COMPATIBLE = {".xlsm": {".xlsx"}, ".xlsx": {".xlsm"}, ".tiff": {".tif"}, ".jpeg": {".jpg"}}


def fix_extension(name: str, data: bytes) -> str:
    """Добавляет недостающее расширение и исправляет неверное (например, .doc, который на деле RTF или .docx)."""
    sniffed = sniff_suffix(data)
    suffix = known_suffix(name)
    if not sniffed:
        return name
    if suffix == sniffed or sniffed in _COMPATIBLE.get(suffix, set()):
        return name
    if suffix in SIGNATURES or (suffix in (".txt", ".csv", ".xml") and sniffed == ".html"):
        return name
    stem = name[: len(name) - len(suffix)] if suffix else name
    return stem + sniffed


def _unique(name: str, used: set[str]) -> str:
    """«НМЦК.pdf» второй раз → «НМЦК (2).pdf» (в пределах одного скачивания или архива)."""
    if name.lower() not in used:
        used.add(name.lower())
        return name
    suffix = known_suffix(name)
    stem = name[: len(name) - len(suffix)] if suffix else name
    n = 2
    while f"{stem} ({n}){suffix}".lower() in used:
        n += 1
    result = f"{stem} ({n}){suffix}"
    used.add(result.lower())
    return result


def _zip_member_name(info: zipfile.ZipInfo) -> str:
    raw = info.filename
    if info.flag_bits & 0x800:
        return raw
    try:  # без флага UTF-8 имя прочитано как cp437; на деле это UTF-8 (новые архиваторы) или cp866 (Windows)
        data = raw.encode("cp437")
    except UnicodeEncodeError:
        return raw
    for enc in ("utf-8", "cp866"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw


def unpack_zip(path: Path, dest: Path) -> list[Path]:
    """Распаковывает архив, защищаясь от путей вида ../ и слишком больших файлов."""
    out: list[Path] = []
    used: set[str] = set()
    with zipfile.ZipFile(path) as zf:
        members = [i for i in zf.infolist() if not i.is_dir()]
        for info in members[:MAX_FILES]:
            if info.file_size > MAX_FILE_BYTES:
                continue
            name = _unique(safe_name(Path(_zip_member_name(info).replace("\\", "/")).name, f"file_{len(out) + 1}"), used)
            target = dest / name
            with zf.open(info) as src:
                data = src.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                continue
            fixed = fix_extension(name, data)
            if fixed != name:
                target = dest / _unique(fixed, used)
            target.write_bytes(data)
            out.append(target)
    return out


def process_files(paths: list[Path], work_dir: Path) -> tuple[list[TenderFile], str]:
    """Извлекает текст из файлов. Возвращает список файлов со статусом и общий текст.

    Ошибка в одном файле (битый .doc, неудачная конвертация, скан) не останавливает разбор остальных.
    """
    files: list[TenderFile] = []
    texts: list[tuple[str, str]] = []
    hashes: dict[str, str] = {}
    queue = list(paths)
    while queue and len(files) < MAX_FILES * 2:
        path = queue.pop(0)
        item = TenderFile(name=path.name, path=str(path))
        suffix = path.suffix.lower()
        try:
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ValueError("файл больше 40 МБ")
            if size == 0:
                raise ValueError("пустой файл")
            digest = hashlib.sha1(path.read_bytes()).hexdigest()
            if suffix in SIGNATURES:
                item.status, item.kind = "пропущен: файл электронной подписи", "skipped"
            elif digest in hashes:
                item.status, item.kind = f"пропущен: копия файла «{hashes[digest]}»", "skipped"
            elif suffix in IMAGES:
                item.status, item.kind = "пропущен: изображение (скан) — текст из картинок не извлекается", "warning"
            elif suffix in UNSUPPORTED_HINT:
                item.status, item.kind = f"ошибка: {UNSUPPORTED_HINT[suffix]}", "error"
            elif suffix == ".zip":
                hashes[digest] = path.name
                inner_dir = work_dir / safe_name(path.stem + "_zip")
                inner_dir.mkdir(parents=True, exist_ok=True)
                inner = unpack_zip(path, inner_dir)
                queue.extend(inner)
                item.status = f"архив: {len(inner)} файлов"
            else:
                hashes[digest] = path.name
                text = extract_text(path).strip()[:MAX_TEXT_CHARS]
                item.chars = len(text)
                if text:
                    texts.append((path.name, text))
                    item.status = "текст извлечён"
                else:
                    item.status, item.kind = "текст не найден (возможно, скан — нужен файл с текстом)", "warning"
        except (ValueError, OSError, zipfile.BadZipFile) as exc:
            item.status, item.kind = f"ошибка: {exc}", "error"
        except Exception as exc:  # повреждённый файл не должен ронять разбор остальных
            item.status, item.kind = f"ошибка чтения ({exc.__class__.__name__}): файл повреждён или в неизвестном формате", "error"
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


# html.parser превращает «&not», «&para» и т.п. внутри href без «;» в символы: «&noticeGuid» → «¬iceGuid».
_ENTITY_FIX = {"¬": "&not", "¶": "&para", "©": "&copy", "®": "&reg", "§": "&sect", "¢": "&cent",
               "£": "&pound", "¤": "&curren", "µ": "&micro", "¥": "&yen", "°": "&deg"}


def fix_href(href: str) -> str:
    for char, entity in _ENTITY_FIX.items():
        href = href.replace(char, entity)
    return href


_SIZE_RE = re.compile(r"\s*[\(\[]?\s*\d[\d\s.,]*\s*(?:[кКkKмМmMгГgG][бБbB]|байт|bytes?|[бБbB])\s*[\)\]]?\s*$")
_GENERIC_LABELS = {"скачать", "загрузить", "файл", "открыть", "download", "документ", "просмотр"}
_FILE_IN_TEXT = re.compile(r"[^\\/:*?\"<>|\n\r\t]{1,200}?\.(?:docx?|xlsx?|xlsm|pdf|rtf|zip|rar|7z|txt|csv|odt|ods|sig|p7s)\b", re.I)


def _clean_label(text: str) -> str:
    text = " ".join((text or "").split())
    text = _SIZE_RE.sub("", text).strip(" -–—:")
    return "" if text.lower() in _GENERIC_LABELS else text


def link_name(link) -> str:
    """Имя файла для ссылки: title, текст ссылки или имя рядом в той же строке таблицы — что содержит расширение."""
    labels = [_clean_label(link.get(attr) or "") for attr in ("title", "data-tooltip", "aria-label", "download")]
    labels.append(_clean_label(link.get_text(" ", strip=True)))
    labels = [x for x in labels if x]
    for label in labels:
        if known_suffix(label):
            return label
    parent = link.find_parent(["tr", "li", "td"]) or link.parent
    if parent is not None:
        match = _FILE_IN_TEXT.search(" ".join(parent.get_text(" ", strip=True).split()))
        if match:
            return match.group(0).strip()
    return labels[0] if labels else ""


def parse_document_links(html: str, page_url: str) -> list[tuple[str, str]]:
    """Ссылки на файлы закупки: (имя файла, адрес)."""
    soup = BeautifulSoup(html, "html.parser")
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        href = fix_href(link["href"]).strip()
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
        found.append((safe_name(link_name(link), f"document_{len(found) + 1}"), url))
    return found[:MAX_FILES]


def find_documents_tab(card_html: str, card_url: str) -> str | None:
    """Ссылка на вкладку «Документы» из карточки закупки (у 223-ФЗ в ней внутренний код, а не номер)."""
    soup = BeautifulSoup(card_html, "html.parser")
    for link in soup.find_all("a", href=True):
        href = fix_href(link["href"]).strip()
        text = link.get_text(" ", strip=True).lower()
        if ("documents" in href.lower() and ".html" in href.lower()) or text == "документы":
            if href.lower().startswith(("javascript:", "#")):
                continue
            url = urljoin(card_url, href)
            if urlparse(url).netloc.endswith("zakupki.gov.ru"):
                return url
    return None


def locate_documents(client: ProcurementClient, purchase_url: str) -> tuple[str, list[tuple[str, str]], list[str]]:
    """Ищет файлы закупки. Возвращает (страница, где нашлись, ссылки, журнал попыток)."""
    if not purchase_url:
        raise ProcurementError("У закупки нет ссылки на ЕИС. Добавьте файлы кнопкой «Выбрать файлы».")
    tried: list[str] = []
    card_html = ""
    blocked = ""
    try:
        card_html = client.get_text(purchase_url)
        blocked = blocked_reason(card_html) if not parse_document_links(card_html, purchase_url) else ""
        tried.append(f"карточка: OK ({purchase_url})" + (f" — заглушка: {blocked}" if blocked else ""))
    except ProcurementError as exc:
        tried.append(f"карточка: {exc} ({purchase_url})")
    candidates: list[str] = []
    tab = find_documents_tab(card_html, purchase_url) if card_html else None
    if tab:
        candidates.append(tab)
    guessed = documents_page_url(purchase_url)
    if guessed not in candidates:
        candidates.append(guessed)
    last_error: ProcurementError | None = None
    for page_url in candidates:
        try:
            html = client.get_text(page_url)
        except ProcurementError as exc:
            last_error = exc
            tried.append(f"{exc} ({page_url})")
            continue
        links = parse_document_links(html, page_url)
        tried.append(f"файлов: {len(links)} ({page_url})")
        if links:
            return page_url, links, tried
        blocked = blocked or blocked_reason(html)
    if card_html:
        links = parse_document_links(card_html, purchase_url)  # некоторые карточки сразу содержат файлы
        if links:
            return purchase_url, links, tried
    if blocked:
        raise ProcurementError(blocked_message(blocked))
    if last_error is not None and not card_html:
        raise last_error
    raise ProcurementError(
        "На странице документов закупки не нашлось файлов. Скачайте ТЗ с сайта вручную "
        "и добавьте кнопкой «Выбрать файлы»."
    )


def _mojibake(name: str) -> bool:
    """«ÐŸÑ€Ð¸Ð»Ð¾…» — имя в неверной кодировке."""
    return len(re.findall(r"[\u00c0-\u00ff]", name)) >= 3 and not re.search(r"[А-Яа-яЁё]", name)


def download_documents(client: ProcurementClient, links: list[tuple[str, str]], dest: Path,
                       errors: list[str] | None = None) -> list[Path]:
    """Скачивает файлы по ссылкам. Ошибка одного файла не останавливает остальные: причины — в errors.

    Имя: из ссылки (title/текст), если в нём есть расширение, иначе из Content-Disposition; расширение проверяется
    по содержимому; одинаковые имена (НМЦК.xlsx и НМЦК.pdf без расширений, разные редакции) не перезаписывают
    друг друга; одинаковые по содержимому файлы сохраняются один раз.
    """
    errors = errors if errors is not None else []
    dest.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    used: set[str] = set()
    digests: set[str] = set()
    blocked_in_row = 0
    for index, (name, url) in enumerate(links, start=1):
        label = name or f"файл {index}"
        try:
            data, header_name = client.get_file(url)
        except ProcurementError as exc:
            errors.append(f"{label}: {exc}")
            blocked_in_row = blocked_in_row + 1 if "заглушку" in str(exc) else 0
            if blocked_in_row >= 2:
                errors.append("Остальные файлы не скачивались: ЕИС блокирует запросы.")
                break
            continue
        blocked_in_row = 0
        if len(data) > MAX_FILE_BYTES:
            errors.append(f"{label}: файл больше 40 МБ — скачайте его вручную")
            continue
        digest = hashlib.sha1(data).hexdigest()
        if digest in digests:
            continue  # та же редакция файла, указанная на странице дважды
        digests.add(digest)
        header_name = "" if _mojibake(header_name or "") else (header_name or "")
        if known_suffix(name):
            final = name
        elif known_suffix(header_name):
            final = header_name
        else:
            final = name or header_name or f"document_{index}"
        final = _unique(safe_name(fix_extension(safe_name(final, f"document_{index}"), data), f"document_{index}"), used)
        target = dest / final
        try:
            target.write_bytes(data)
        except OSError as exc:
            errors.append(f"{final}: не удалось сохранить ({exc.strerror or exc.__class__.__name__})")
            continue
        saved.append(target)
    return saved


class Downloaded(list):
    """Список скачанных файлов; в .errors — причины, по которым часть файлов не скачалась."""

    def __init__(self, paths: list[Path], errors: list[str]) -> None:
        super().__init__(paths)
        self.errors = errors


def fetch_eis_documents(client: ProcurementClient, purchase_url: str, dest: Path,
                        errors: list[str] | None = None) -> Downloaded:
    """Находит и скачивает файлы закупки. Если не скачался ни один — ProcurementError с причиной."""
    _, links, _ = locate_documents(client, purchase_url)
    errors = errors if errors is not None else []
    saved = download_documents(client, links, dest, errors)
    if not saved:
        detail = "; ".join(errors[:3]) or "нет файлов"
        raise ProcurementError(f"Не удалось скачать файлы закупки: {detail}")
    return Downloaded(saved, errors)


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
