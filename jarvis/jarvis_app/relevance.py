"""Фильтр релевантности закупок: оставляет только то, что относится к вашим тегам.

Поиск ЕИС находит слово где угодно — в документации, лотах, морфологических вариантах, — поэтому в выдачу
попадает, например, медицинское оборудование «с мультимедийным модулем». Здесь проверяется название закупки:
- строгий режим: в названии есть все значимые слова тега в любой словоформе («проектор» → «проекторов»);
- минус-слова: если в названии есть хоть одно (по началу слова), закупка отбрасывается.
"""

from __future__ import annotations

import re

STOP_WORDS = {"для", "и", "в", "во", "на", "по", "с", "со", "к", "из", "от", "до", "или", "а", "у", "о", "об"}

# «питани» как минус-слово скрывало «источник бесперебойного питания» и «блок питания» — оставлены только фразы.
# Окончания, которые отрезаются, чтобы «театр», «театра», «театральное» совпадали. От длинных к коротким.
_ENDINGS = sorted(
    "иями ями ами ого его ому ему ыми ими иях ях ах ов ев ей ий ый ой ая яя ое ее ые ие ых их ым им ом ем "
    "ую юю ия ья ие ье а я ы и у ю е о ь й".split(),
    key=len,
    reverse=True,
)

DEFAULT_MINUS_WORDS = [
    "медицин", "хирург", "стоматолог", "рентген", "томограф", "ультразвук", "узи", "эндоскоп", "реанимац",
    "лаборатор", "реагент", "лекарств", "фармац", "диагностич", "больниц", "поликлиник", "санитар", "дезинфек",
    "ветеринар", "дорог", "асфальт", "продукт", "продуктов питания", "организация питания", "услуги питания", "топлив", "бензин", "уголь", "строительств",
    "капитальн", "канцеляр", "мебел", "уборк", "охран", "страхован", "автомобил", "шин",
]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


def words(text: str) -> list[str]:
    return re.findall(r"[a-zа-я0-9]+", normalize(text))


def stem(word: str) -> str:
    word = normalize(word)
    if len(word) <= 4:
        return word
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 4:
            return word[: -len(ending)]
    return word


def tag_stems(tag: str) -> list[str]:
    return [stem(w) for w in words(tag) if w not in STOP_WORDS]


def tag_matches(title: str, tag: str) -> bool:
    """Все значимые слова тега есть в названии (слово названия начинается с основы слова тега)."""
    stems = tag_stems(tag)
    if not stems:
        return False
    title_words = words(title)
    return all(any(w.startswith(s) for w in title_words) for s in stems)


def minus_hit(title: str, minus_words: list[str]) -> str:
    """Первое найденное минус-слово или пустая строка."""
    title_words = words(title)
    for raw in minus_words or []:
        m = normalize(raw)
        if not m:
            continue
        if " " in m:
            if m in normalize(title):
                return raw
        elif any(w.startswith(m) for w in title_words):
            return raw
    return ""


def parse_minus_words(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,;\n]", value)
    return [str(v).strip() for v in value or [] if str(v).strip()]
