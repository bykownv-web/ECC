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
