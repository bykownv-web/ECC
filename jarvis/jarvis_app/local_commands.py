"""Простые команды, которые работают без ИИ и без API-ключа.

parse_command возвращает (имя_действия, аргументы) или None, если команда не распознана.
"""

from __future__ import annotations

import re
from typing import Any

_UNITS = {
    "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
    "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
    "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
    "восемнадцать": 18, "девятнадцать": 19,
}
_TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50}
_NUMBER_WORDS = {**_UNITS, **_TENS}
# «через минуту», «через час» — число без отдельного слова единицы
_BARE_WORDS = {"минуту": 1, "минутку": 1, "час": 60, "полчаса": 30, "полтора": 90}

_UNIT_RE = r"(минут\w*|мин\b|час\w*|секунд\w*|сек\b)"
_TIME_EXPR = re.compile(
    rf"(?:через\s+|на\s+)?(?:\d+(?:[.,]\d+)?\s*{_UNIT_RE}(?:\s+(?:и\s+)?\d+\s*(?:минут\w*|мин\b))?"
    rf"|(?:(?:{'|'.join(_TENS)})\s+)?(?:{'|'.join(_NUMBER_WORDS)})\s+{_UNIT_RE}"
    r"|полтора\s+час\w*|полчаса"
    r"|через\s+(?:минуту|минутку|час))"
)

_REMINDER_START = re.compile(
    r"^(напомни\w*|поставь\s+(таймер|будильник|напоминани\w*)|таймер|будильник|засеки)\b"
)

ZAKUPKI_URL = "https://zakupki.gov.ru"


def _unit_factor(unit: str) -> float:
    if unit.startswith("час"):
        return 60
    if unit.startswith("сек"):
        return 1 / 60
    return 1


def _minutes(text: str) -> float | None:
    match = re.search(rf"(\d+(?:[.,]\d+)?)\s*{_UNIT_RE}", text)
    if match:
        value = float(match.group(1).replace(",", "."))
        unit = match.group(2)
        result = value * _unit_factor(unit)
        if unit.startswith("час"):  # «через 2 часа 30 минут»
            extra = re.match(r"\s+(?:и\s+)?(\d+)\s*(?:минут\w*|мин\b)", text[match.end():])
            if extra:
                result += float(extra.group(1))
        if unit.startswith("сек"):
            result = max(result, 1 / 60)
        return result
    if re.search(r"полтора\s+час", text):
        return 90
    if "полчаса" in text:
        return 30
    words = "|".join(_NUMBER_WORDS)
    match = re.search(rf"(?:\b({'|'.join(_TENS)})\s+)?\b({words})\s+{_UNIT_RE}", text)
    if match:
        value = _NUMBER_WORDS[match.group(2)] + (_TENS[match.group(1)] if match.group(1) else 0)
        return value * _unit_factor(match.group(3))
    match = re.search(r"через\s+(\w+)", text)
    if match and match.group(1) in _BARE_WORDS:
        return _BARE_WORDS[match.group(1)]
    return None


def _reminder(t: str) -> tuple[str, dict[str, Any]] | None:
    minutes = _minutes(t)
    if not minutes:
        return None
    rest = _TIME_EXPR.sub(" ", t, count=1)
    rest = re.sub(r"^\s*(напомни\w*|поставь\s+(таймер|будильник|напоминани\w*)|таймер|будильник|засеки)\b", "", rest)
    rest = re.sub(r"^\s*(мне|нам)\b", "", rest)
    rest = re.sub(r"^\s*(на|через)\s*$", "", rest)
    reminder = " ".join(rest.split()).strip(" ,.") or "время вышло"
    return "set_reminder", {"text": reminder, "minutes": minutes}


def _parse_price(raw: str) -> tuple[str, dict[str, float]]:
    """«до 5 млн», «от 300 тысяч» → price_to/price_from; возвращает строку без этих слов."""
    found: dict[str, float] = {}
    pattern = re.compile(
        r"\b(от|до|дороже|дешевле)\s+(\d[\d\s]*(?:[.,]\d+)?)\s*(млн\w*|миллион\w*|тыс\w*|т\.?\s?р\.?|руб\w*|р\.?)?(?=\s|$)"
    )

    def repl(match: re.Match[str]) -> str:
        value = float(re.sub(r"\s", "", match.group(2)).replace(",", "."))
        unit = match.group(3) or ""
        if unit.startswith(("млн", "миллион")):
            value *= 1_000_000
        elif unit.startswith(("тыс", "т")):
            value *= 1_000
        found["price_from" if match.group(1) in ("от", "дороже") else "price_to"] = value
        return " "

    return pattern.sub(repl, raw), found


def _procurement(t: str) -> tuple[str, dict[str, Any]]:
    if re.search(r"(excel|эксель|выгруз|экспорт|сохрани)", t):
        return "export_procurements", {}
    if re.search(r"(аналитик|статистик|сводк)", t):
        return "procurement_analytics", {}
    args: dict[str, Any] = {}
    match = re.search(r"(?:закупк\w*|тендер\w*|аукцион\w*)\s+(.+)$", t)
    tags: list[str] = []
    if match:
        raw = match.group(1)
        raw = re.sub(r"\b(по\s+)?(44|223)\s?-?\s?фз\b", " ", raw)
        raw, prices = _parse_price(raw)
        args.update(prices)
        raw = re.sub(r"\b(за|на)\s+(сегодня|вчера|неделю|месяц|последн\w+\s+\w+)\b", " ", raw)
        raw = " ".join(raw.split())
        raw = re.sub(r"^(по|на|для)\s+", "", raw)
        raw = re.sub(r"^((мо\w+|сохран\w+|наш\w+)\s+)?(тег\w*|ключев\w+\s+слов\w*|слов\w*)(\s+|$)", "", raw)
        raw = re.sub(r"^(на\s+)?(поставк\w*|приобретени\w*|покупк\w*)\s+", "", raw)
        stop = {"новые", "текущие", "все", "свежие", "последние", "сегодня", "актуальные", "мне", "пожалуйста"}
        candidates = [s.strip(" .") for s in re.split(r",|\sи\s", raw) if s.strip(" .")]
        tags = [c for c in candidates if c not in stop]
    if tags:
        args = {"tags": tags, **args}
    if re.search(r"44.?фз", t) and not re.search(r"223.?фз", t):
        args["fz223"] = False
    if re.search(r"223.?фз", t) and not re.search(r"44.?фз", t):
        args["fz44"] = False
    return "search_procurements", args


def _tender_number(t: str) -> str | None:
    for match in re.finditer(r"(?<!\d)(\d{19}|\d{11})(?!\d)", t):
        number = match.group(1)
        if len(number) == 11 and number[0] in "78":
            # 89161234567 — это телефон; номер закупки по 223-ФЗ так не начинается, если нет слова «закупка»
            if not re.search(r"(закупк|тендер|аукцион|извещени|заявк)", t):
                continue
        return number
    return None


def parse_command(text: str) -> tuple[str, dict[str, Any]] | None:
    t = " ".join((text or "").lower().replace("ё", "е").split()).strip(" .!?")
    if not t:
        return None

    # Напоминание — первым: внутри него могут быть и «закупки», и «время», и номер телефона.
    if _REMINDER_START.match(t):
        reminder = _reminder(t)
        if reminder:
            return reminder

    link = re.search(r"https?://\S*zakupki\.gov\.ru\S*", text or "")
    if link:
        return "prepare_tender", {"number": link.group(0)}
    number = _tender_number(t)
    if number:
        return "prepare_tender", {"number": number}
    if re.search(r"(разбор|разбери|разобрать|проанализируй|проверь)\w*\s+(тз|техническ\w* задани|документац|закупк\w* целиком)", t):
        return "prepare_tender", {}

    # «открой закупки» — это сайт ЕИС, а не поиск
    if re.fullmatch(
        r"(открой|запусти|зайди на|перейди на)\s+(сайт\s+)?(гос\s?закупк\w*|закупк\w*|еис|zakupki(\.gov\.ru)?)(\s+гов\s+ру)?", t
    ):
        return "open_url", {"url": ZAKUPKI_URL}

    if re.search(r"\b(закупк|тендер|аукцион|44.?фз|223.?фз)", t):
        return _procurement(t)

    if re.search(r"(который час|сколько времени|какое (сегодня )?число|какой (сегодня )?день|какая (сегодня )?дата)", t) or \
            re.fullmatch(r"((скажи|подскажи)\s+)?((текущее|точное)\s+)?время(\s+сейчас)?", t):
        return "get_time", {}

    if "погод" in t:
        match = re.search(r"погод\w*\s+(?:в|во)\s+(.+)$", t)
        city = re.sub(r"\s+(сегодня|завтра|сейчас|на улице)$", "", match.group(1)).strip() if match else ""
        return "get_weather", ({"city": city} if city else {})

    if re.search(r"(отмени|отменить|отмена)\s+(выключени|перезагруз)", t):
        return "power", {"action": "cancel"}
    if re.search(r"(выключи|выруби)\s+(компьютер|пк|комп)", t):
        return "power", {"action": "shutdown"}
    if re.search(r"(перезагрузи|перезапусти)\s+(компьютер|пк|комп)", t):
        return "power", {"action": "restart"}
    if re.search(r"(заблокируй|блокировка)", t):
        return "lock_screen", {}

    if re.search(r"(громче|прибавь звук|увеличь громкость)", t):
        return "set_volume", {"action": "up"}
    if re.search(r"(тише|убавь звук|уменьши громкость)", t):
        return "set_volume", {"action": "down"}
    if re.search(r"(выключи звук|включи звук|без звука)", t):
        return "set_volume", {"action": "mute"}
    if re.search(r"\b(пауза|паузу)\b", t) or re.fullmatch(
        r"(продолжи\w*|возобнови\w*)(\s+(воспроизведение|музыку|песню|трек))?|play|плей", t
    ):
        return "media_control", {"action": "play_pause"}
    if re.search(r"следующ\w* (трек|песн)", t):
        return "media_control", {"action": "next"}
    if re.search(r"предыдущ\w* (трек|песн)", t):
        return "media_control", {"action": "prev"}

    if re.search(r"(напомни|таймер|будильник)", t):
        reminder = _reminder(t)
        if reminder:
            return reminder
    if re.search(r"(какие|список) напоминани", t):
        return "list_reminders", {}

    if re.search(r"(загрузк\w* (процессора|компьютера)|состояни\w* (системы|компьютера)|сколько памяти)", t):
        return "system_info", {}

    match = re.match(r"(?:найди|поищи|загугли)\s+(?:в интернете\s+)?(.+)$", t)
    if match:
        return "web_search", {"query": match.group(1)}

    match = re.match(r"(?:открой|запусти|включи)\s+(?:программу\s+|сайт\s+)?(.+)$", t)
    if match:
        target = match.group(1).strip()
        if re.match(r"https?://", target):
            return "open_url", {"url": target}
        if re.fullmatch(r"(www\.)?[a-zа-я0-9-]+(\.[a-zа-я0-9-]+)*\.(ru|com|рф|org|net|io|su|by|kz|me|tv|info)(/\S*)?", target):
            return "open_url", {"url": f"https://{target}"}
        return "open_app", {"name": target}

    return None
