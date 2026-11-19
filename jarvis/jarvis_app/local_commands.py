"""Простые команды, которые работают без ИИ и без API-ключа.

parse_command возвращает (имя_действия, аргументы) или None, если команда не распознана.
"""

from __future__ import annotations

import re
from typing import Any

_NUMBER_WORDS = {
    "одну": 1, "одна": 1, "один": 1, "минуту": 1, "две": 2, "два": 2, "три": 3, "четыре": 4,
    "пять": 5, "десять": 10, "пятнадцать": 15, "двадцать": 20, "тридцать": 30, "полчаса": 30,
    "час": 60, "сорок": 40, "пятьдесят": 50,
}


def _minutes(text: str) -> float | None:
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(минут|мин|час|сек)", text)
    if match:
        value = float(match.group(1).replace(",", "."))
        unit = match.group(2)
        if unit.startswith("час"):
            return value * 60
        if unit.startswith("сек"):
            return max(value / 60, 1 / 60)
        return value
    if "полчаса" in text:
        return 30
    match = re.search(r"через\s+(\w+)\s*(минут|час)?", text)
    if match and match.group(1) in _NUMBER_WORDS:
        value = _NUMBER_WORDS[match.group(1)]
        if match.group(2) and match.group(2).startswith("час") and value != 60:
            value *= 60
        return value
    return None


def parse_command(text: str) -> tuple[str, dict[str, Any]] | None:
    t = " ".join((text or "").lower().replace("ё", "е").split()).strip(" .!?")
    if not t:
        return None

    link = re.search(r"https?://\S*zakupki\.gov\.ru\S*", text or "")
    number = re.search(r"(?<!\d)(\d{19}|\d{11})(?!\d)", t)
    if link:
        return "prepare_tender", {"number": link.group(0)}
    if number:
        return "prepare_tender", {"number": number.group(1)}
    if re.search(r"(разбор|разбери|разобрать|проанализируй|проверь)\w*\s+(тз|техническ\w* задани|документац|закупк\w* целиком)", t):
        return "prepare_tender", {}

    if re.search(r"\b(закупк|тендер|аукцион|44.?фз|223.?фз)", t):
        if re.search(r"(excel|эксель|выгруз|экспорт|сохрани)", t):
            return "export_procurements", {}
        if re.search(r"(аналитик|статистик|сводк)", t):
            return "procurement_analytics", {}
        match = re.search(r"(?:закупк\w*|тендер\w*)\s+(?:по\s+|на\s+)?(.+)$", t)
        tags: list[str] | None = None
        if match:
            raw = re.sub(r"^(тег\w*|слов\w*)\s+", "", match.group(1))
            candidates = [s.strip() for s in re.split(r",|\sи\s", raw) if s.strip()]
            stop = {"44 фз", "223 фз", "44-фз", "223-фз", "новые", "текущие", "все"}
            tags = [c for c in candidates if c not in stop] or None
        args: dict[str, Any] = {}
        if tags:
            args["tags"] = tags
        if re.search(r"44.?фз", t) and not re.search(r"223.?фз", t):
            args["fz223"] = False
        if re.search(r"223.?фз", t) and not re.search(r"44.?фз", t):
            args["fz44"] = False
        return "search_procurements", args

    if re.search(r"(который час|сколько времени|какое (сегодня )?число|какой (сегодня )?день|время)\b", t):
        return "get_time", {}

    if "погод" in t:
        match = re.search(r"погод\w*\s+(?:в|во)\s+(.+)$", t)
        return "get_weather", ({"city": match.group(1).strip()} if match else {})

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
    if re.search(r"(пауза|продолжи|play|плей)", t):
        return "media_control", {"action": "play_pause"}
    if re.search(r"следующ\w* (трек|песн)", t):
        return "media_control", {"action": "next"}
    if re.search(r"предыдущ\w* (трек|песн)", t):
        return "media_control", {"action": "prev"}

    if re.search(r"(напомни|таймер|будильник)", t):
        minutes = _minutes(t)
        if minutes:
            match = re.search(r"напомни\w*(?:\s+мне)?\s+(?:через\s+\S+(?:\s+\S*(?:минут|час|сек)\w*)?\s+)?(.*)$", t)
            reminder = (match.group(1) if match else "").strip()
            reminder = re.sub(r"^(через\s+)?[\d.,]+\s*\w*\s*", "", reminder).strip() or "время вышло"
            return "set_reminder", {"text": reminder, "minutes": minutes}
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
        return "open_app", {"name": target}

    return None
