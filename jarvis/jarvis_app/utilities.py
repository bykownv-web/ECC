"""Полезные мелочи: время, дата, погода (Open-Meteo, без ключа) и напоминания."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

import requests

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

WEATHER_CODES = {
    0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "изморозь", 51: "лёгкая морось", 53: "морось", 55: "сильная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь", 66: "ледяной дождь", 67: "сильный ледяной дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
    80: "ливень", 81: "сильный ливень", 82: "очень сильный ливень",
    85: "снегопад", 86: "сильный снегопад", 95: "гроза", 96: "гроза с градом", 99: "сильная гроза с градом",
}


def now_text() -> str:
    now = datetime.now()
    return (
        f"Сейчас {now:%H:%M}, {WEEKDAYS[now.weekday()]}, "
        f"{now.day} {MONTHS[now.month - 1]} {now.year} года."
    )


def _city_variants(city: str) -> list[str]:
    """«москве» → «москва», «казани» → «казань»: голосом город часто называют в предложном падеже."""
    variants = [city]
    low = city.lower()
    if len(low) > 3:
        if low.endswith("е"):
            variants += [city[:-1] + "а", city[:-1]]
        elif low.endswith("и"):
            variants += [city[:-1] + "ь", city[:-1] + "а", city[:-1] + "я"]
        elif low.endswith("у"):
            variants += [city[:-1] + "а"]
    return list(dict.fromkeys(variants))


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _geocode(city: str) -> dict | None:
    for name in _city_variants(city)[:4]:
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": name, "count": 1, "language": "ru", "format": "json"},
            timeout=15,
        ).json()
        results = geo.get("results") if isinstance(geo, dict) else None
        if results and isinstance(results[0], dict) and "latitude" in results[0] and "longitude" in results[0]:
            return results[0]
    return None


def weather(city: str) -> str:
    city = (city or "").strip() or "Москва"
    try:
        place = _geocode(city)
        if place is None:
            return f"Не нашёл город «{city}»."
        data = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min",
                "wind_speed_unit": "ms",
                "timezone": "auto",
                "forecast_days": 1,
            },
            timeout=15,
        ).json()
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return "Сервис погоды сейчас недоступен."
    if not isinstance(data, dict) or data.get("error") or not isinstance(data.get("current"), dict):
        return "Сервис погоды сейчас недоступен."
    current = data["current"]
    daily = data.get("daily") if isinstance(data.get("daily"), dict) else {}
    code = _num(current.get("weather_code"))
    description = WEATHER_CODES.get(int(code), "") if code is not None else ""
    temp = _num(current.get("temperature_2m"))
    feels = _num(current.get("apparent_temperature"))
    wind = _num(current.get("wind_speed_10m"))
    tmax = _num((daily.get("temperature_2m_max") or [None])[0])
    tmin = _num((daily.get("temperature_2m_min") or [None])[0])
    name = place.get("name") or city
    parts = [f"В городе {name} сейчас {temp:+.0f}°" if temp is not None else f"Погода: {name}"]
    if description:
        parts.append(description)
    if feels is not None:
        parts.append(f"ощущается как {feels:+.0f}°")
    if wind is not None:
        parts.append(f"ветер {wind:.0f} м/с")
    if tmax is not None and tmin is not None:
        parts.append(f"днём от {tmin:+.0f}° до {tmax:+.0f}°")
    return ", ".join(parts) + "."


@dataclass
class Reminder:
    id: str
    text: str
    due: datetime
    timer: threading.Timer


class ReminderManager:
    def __init__(self, on_fire: Callable[[str], None]) -> None:
        self._on_fire = on_fire
        self._items: dict[str, Reminder] = {}
        self._lock = threading.Lock()

    def add(self, text: str, minutes: float) -> str:
        try:
            minutes = float(minutes)
        except (TypeError, ValueError) as exc:
            raise ValueError("Через сколько минут напомнить? Назовите число.") from exc
        if not (0 < minutes <= 60 * 24 * 7):  # заодно отсекает NaN
            raise ValueError("Напоминание можно поставить от 1 секунды до 7 дней.")
        text = str(text or "")
        rid = uuid.uuid4().hex[:8]
        timer = threading.Timer(minutes * 60, self._fire, args=(rid,))
        timer.daemon = True
        due = datetime.now() + timedelta(minutes=minutes)
        with self._lock:
            self._items[rid] = Reminder(rid, text.strip() or "Время вышло", due, timer)
        timer.start()
        when = f"в {due:%H:%M}" if due.date() == datetime.now().date() else f"{due:%d.%m} в {due:%H:%M}"
        return f"Напомню {when}: {text.strip() or 'время вышло'}."

    def _fire(self, rid: str) -> None:
        with self._lock:
            item = self._items.pop(rid, None)
        if item is not None:
            try:
                self._on_fire(item.text)
            except Exception:  # поток таймера: исключение не должно пропасть молча
                import traceback

                traceback.print_exc()

    def list(self) -> list[dict[str, str]]:
        with self._lock:
            return [
                {"id": r.id, "text": r.text, "due": f"{r.due:%H:%M}"}
                for r in sorted(self._items.values(), key=lambda r: r.due)
            ]

    def cancel_all(self) -> int:
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
        for item in items:
            item.timer.cancel()
        return len(items)
