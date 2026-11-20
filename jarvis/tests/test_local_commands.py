import pytest

from jarvis_app.local_commands import parse_command
from jarvis_app.voice import extract_after_wake_word


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Который час?", ("get_time", {})),
        ("какая погода в Казани", ("get_weather", {"city": "казани"})),
        ("погода", ("get_weather", {})),
        ("открой блокнот", ("open_app", {"name": "блокнот"})),
        ("открой https://ya.ru", ("open_url", {"url": "https://ya.ru"})),
        ("сделай громче", ("set_volume", {"action": "up"})),
        ("выключи звук", ("set_volume", {"action": "mute"})),
        ("выключи компьютер", ("power", {"action": "shutdown"})),
        ("отмени выключение", ("power", {"action": "cancel"})),
        ("найди в интернете рецепт борща", ("web_search", {"query": "рецепт борща"})),
        ("найди закупки", ("search_procurements", {})),
        ("найди закупки проектор и звуковое оборудование", ("search_procurements", {"tags": ["проектор", "звуковое оборудование"]})),
        ("закупки по 223-ФЗ", ("search_procurements", {"fz44": False})),
        ("выгрузи закупки в excel", ("export_procurements", {})),
        ("аналитика по закупкам", ("procurement_analytics", {})),
    ],
)
def test_parse_command(text, expected):
    assert parse_command(text) == expected


def test_reminder_parsing():
    name, args = parse_command("напомни через 15 минут позвонить заказчику")
    assert name == "set_reminder"
    assert args == {"text": "позвонить заказчику", "minutes": 15}
    name, args = parse_command("напомни через пять минут выключить чайник")
    assert args["minutes"] == 5 and args["text"] == "выключить чайник"
    name, args = parse_command("поставь таймер на 2 часа")
    assert args["minutes"] == 120


def test_unknown_command():
    assert parse_command("расскажи анекдот") is None
    assert parse_command("") is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Джарвис, который час", "который час"),
        ("джарвис", ""),
        ("Эй Джарвису открой блокнот", "открой блокнот"),
        ("который час", None),
    ],
)
def test_wake_word(text, expected):
    assert extract_after_wake_word(text, "джарвис") == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("32616576327 вот закупка все проверь", ("prepare_tender", {"number": "32616576327"})),
        ("разбери 0318100005226000001", ("prepare_tender", {"number": "0318100005226000001"})),
        ("проверь https://zakupki.gov.ru/epz/order/notice/zk20/view/common-info.html?regNumber=0318100005226000001",
         ("prepare_tender", {"number": "https://zakupki.gov.ru/epz/order/notice/zk20/view/common-info.html?regNumber=0318100005226000001"})),
        ("сделай разбор ТЗ", ("prepare_tender", {})),
        ("найди закупки проектор", ("search_procurements", {"tags": ["проектор"]})),
    ],
)
def test_tender_commands(text, expected):
    assert parse_command(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("открой закупки", ("open_url", {"url": "https://zakupki.gov.ru"})),
        ("открой сайт госзакупки", ("open_url", {"url": "https://zakupki.gov.ru"})),
        ("открой zakupki.gov.ru", ("open_url", {"url": "https://zakupki.gov.ru"})),
        ("открой ya.ru", ("open_url", {"url": "https://ya.ru"})),
        ("найди закупки на проекторы", ("search_procurements", {"tags": ["проекторы"]})),
        ("найди аукционы на проекторы и экраны", ("search_procurements", {"tags": ["проекторы", "экраны"]})),
        ("найди закупки на поставку проекторов", ("search_procurements", {"tags": ["проекторов"]})),
        ("найди закупки проектор 44-ФЗ", ("search_procurements", {"tags": ["проектор"], "fz223": False})),
        ("найди закупки на проекторы до 5 млн", ("search_procurements", {"tags": ["проекторы"], "price_to": 5_000_000})),
        ("найди закупки на экраны от 300 тысяч", ("search_procurements", {"tags": ["экраны"], "price_from": 300_000})),
        ("найди закупки по моим тегам", ("search_procurements", {})),
        ("найди новые закупки за сегодня", ("search_procurements", {})),
        ("найди время работы магазина", ("web_search", {"query": "время работы магазина"})),
        ("время", ("get_time", {})),
        ("какое сегодня число", ("get_time", {})),
        ("какая погода в москве завтра", ("get_weather", {"city": "москве"})),
        ("позвони 89161234567", None),
        ("закупка 32312345678", ("prepare_tender", {"number": "32312345678"})),
        ("пауза", ("media_control", {"action": "play_pause"})),
        ("открой google play", ("open_app", {"name": "google play"})),
    ],
)
def test_ambiguous_phrases(text, expected):
    assert parse_command(text) == expected


@pytest.mark.parametrize(
    "text,minutes,reminder",
    [
        ("напомни через 10 минут проверить закупки", 10, "проверить закупки"),  # не поиск закупок
        ("напомни через 5 минут позвонить 89161234567", 5, "позвонить 89161234567"),  # не номер закупки
        ("напомни через 10 минут что время обеда", 10, "что время обеда"),  # не «который час»
        ("напомни позвонить маме через 10 минут", 10, "позвонить маме"),
        ("поставь таймер на двадцать пять минут", 25, "время вышло"),
        ("таймер на пять минут", 5, "время вышло"),
        ("напомни через 2 часа 30 минут обед", 150, "обед"),
        ("напомни через полтора часа выйти", 90, "выйти"),
        ("напомни через час про отчёт", 60, "про отчет"),
        ("напомни через минуту", 1, "время вышло"),
        ("напомни через 30 секунд", 0.5, "время вышло"),
        ("напомни купить 2 молока через 10 минут", 10, "купить 2 молока"),
    ],
)
def test_reminders_with_numbers_inside(text, minutes, reminder):
    assert parse_command(text) == ("set_reminder", {"text": reminder, "minutes": minutes})


def test_wake_word_latin_and_yo():
    assert extract_after_wake_word("Jarvis, открой блокнот", "джарвис") == "открой блокнот"
    assert extract_after_wake_word("Джарвис ещё громче", "джарвис") == "ещё громче"
