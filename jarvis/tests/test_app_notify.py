"""Telegram-бот, почта и уведомления: жизненный цикл, ошибки, разбор команд."""

from __future__ import annotations

import threading
import time

import pytest

from jarvis_app import notify
from jarvis_app.config import Config


@pytest.fixture
def secrets(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr(notify, "get_secret", lambda name: store.get(name, ""))
    monkeypatch.setattr(notify, "set_secret", lambda name, value: store.__setitem__(name, value) or True)
    return store


class Resp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def json(self):
        return self._data


class ScriptedTG:
    """Отвечает на методы Telegram по сценарию; getUpdates может «висеть» как длинный опрос."""

    def __init__(self, updates=None, block: threading.Event | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.updates = list(updates or [])
        self.block = block
        self.lock = threading.Lock()

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        with self.lock:
            self.calls.append((method, json))
        if method == "getMe":
            return Resp({"ok": True, "result": {"username": "test_bot"}})
        if method == "getUpdates":
            if self.block is not None:
                self.block.wait(5)
            batch, self.updates = self.updates, []
            if not batch:
                time.sleep(0.01)
            return Resp({"ok": True, "result": batch})
        return Resp({"ok": True, "result": True})

    def count(self, method):
        with self.lock:
            return sum(1 for m, _ in self.calls if m == method)


def msg(chat_id, text, update_id=1):
    return {"update_id": update_id, "message": {"text": text, "chat": {"id": chat_id, "type": "private"},
                                                "from": {"first_name": "Н"}, "date": int(time.time())}}


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_start_without_token_does_nothing(tmp_path, secrets):
    bot = notify.TelegramBot(Config(tmp_path / "c.json"), lambda t: "", lambda e, p: None, session=ScriptedTG())
    bot.start()
    assert bot._thread is None and not bot.status()["running"]


def test_connect_without_any_token_is_an_error(tmp_path, secrets):
    bot = notify.TelegramBot(Config(tmp_path / "c.json"), lambda t: "", lambda e, p: None, session=ScriptedTG())
    with pytest.raises(notify.TelegramError, match="токен"):
        bot.connect("")


def test_connect_with_saved_token_checks_it_and_keeps_pair_code(tmp_path, secrets):
    config = Config(tmp_path / "c.json")
    http = ScriptedTG()
    bot = notify.TelegramBot(config, lambda t: "", lambda e, p: None, session=http)
    bot.start = lambda: None
    first = bot.connect("123:ABC")["code"]
    again = bot.connect("")  # повторное «Подключить» без токена — тот же код, бот берётся из сохранённого
    assert again == {"bot": "test_bot", "code": first}
    assert bot.connect("123:ABC")["code"] == first  # код больше не перегенерируется при каждом подключении


def test_restart_after_disconnect_while_old_poll_still_running(tmp_path, secrets):
    """stop() не убивает поток, висящий в getUpdates; start() раньше видел живой поток и ничего не делал."""
    config = Config(tmp_path / "c.json")
    release = threading.Event()
    http = ScriptedTG(block=release)
    bot = notify.TelegramBot(config, lambda t: "", lambda e, p: None, session=http)
    bot.connect("123:ABC")
    old = bot._thread
    assert wait_for(lambda: http.count("getUpdates") == 1)
    bot.disconnect()
    secrets[notify.TELEGRAM_KEY] = "123:ABC"
    bot.connect("123:ABC")
    new = bot._thread
    assert new is not old and new.is_alive()
    time.sleep(0.1)
    assert http.count("getUpdates") == 1  # новый поток ждёт старый, а не опрашивает параллельно (409)
    release.set()
    assert wait_for(lambda: not old.is_alive())
    assert wait_for(lambda: http.count("getUpdates") >= 2)
    assert new.is_alive()
    bot.stop()


def test_loop_reports_unexpected_errors_instead_of_swallowing(tmp_path, secrets):
    secrets[notify.TELEGRAM_KEY] = "123:ABC"
    events = []
    bot = notify.TelegramBot(Config(tmp_path / "c.json"), lambda t: "", lambda e, p: events.append((e, p)),
                             session=ScriptedTG())

    def broken(offset):
        raise TypeError("post() got an unexpected keyword argument 'http_timeout'")

    bot.poll_once = broken
    bot.start()
    assert wait_for(lambda: events)
    bot.stop()
    event, payload = events[0]
    assert event == "telegram_status" and payload["error"] and "TypeError" in payload["text"]
    assert "TypeError" in bot.status()["last_error"]


def test_409_is_reported_with_code_and_retried_quickly(tmp_path, secrets):
    secrets[notify.TELEGRAM_KEY] = "123:ABC"

    class Conflict(ScriptedTG):
        def post(self, url, json=None, timeout=None):
            method = url.rsplit("/", 1)[1]
            with self.lock:
                self.calls.append((method, json))
            if method == "getUpdates":
                return Resp({"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates request"}, 409)
            return Resp({"ok": True, "result": True})

    events = []
    bot = notify.TelegramBot(Config(tmp_path / "c.json"), lambda t: "", lambda e, p: events.append(p), session=Conflict())
    with pytest.raises(notify.TelegramError) as info:
        bot.poll_once(None)
    assert info.value.code == 409 and "второе окно" in str(info.value)
    waits = []

    class Stop(threading.Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            self.set()
            return True

    bot._loop(Stop())
    assert waits == [15] and events and "второе окно" in events[0]["text"]


def test_poison_update_does_not_repeat_forever(tmp_path, secrets):
    secrets[notify.TELEGRAM_KEY] = "123:ABC"
    config = Config(tmp_path / "c.json")
    config.update({"telegram": {"chat_id": 7}})
    events = []

    def on_message(text):
        raise KeyError("purchase")

    http = ScriptedTG([msg(7, "найди закупки", update_id=50)])
    bot = notify.TelegramBot(config, on_message, lambda e, p: events.append((e, p)), session=http)
    assert bot.poll_once(None) == 51  # offset сдвинут, хотя обработка упала
    assert any(e == "error" and "KeyError" in p["text"] for e, p in events)
    sent = [j["text"] for m, j in http.calls if m == "sendMessage"]
    assert sent and "Не получилось" in sent[-1]


def test_commands_with_bot_suffix(tmp_path, secrets):
    secrets[notify.TELEGRAM_KEY] = "123:ABC"
    config = Config(tmp_path / "c.json")
    config.update({"telegram": {"chat_id": 7, "pair_code": "J-1234"}})
    asked = []
    http = ScriptedTG()
    bot = notify.TelegramBot(config, lambda t: asked.append(t) or "ок", lambda e, p: None, session=http)
    bot.handle_update(msg(7, "/zakupki@test_bot"))
    bot.handle_update(msg(7, "/help@test_bot"))
    assert asked == ["/zakupki"]
    assert any("/zakupki — поиск" in j["text"] for m, j in http.calls if m == "sendMessage")


def test_pairing_with_persisted_code_via_poll(tmp_path, secrets):
    secrets[notify.TELEGRAM_KEY] = "123:ABC"
    config = Config(tmp_path / "c.json")
    config.update({"telegram": {"pair_code": "J-4321"}})
    http = ScriptedTG([msg(9, "/start J4321", update_id=3)])
    bot = notify.TelegramBot(Config(tmp_path / "c.json"), lambda t: "", lambda e, p: None, session=http)
    assert bot.poll_once(None) == 4 and bot.chat_id() == 9 and bot.pair_code == ""


def test_send_splits_long_text(tmp_path, secrets):
    secrets[notify.TELEGRAM_KEY] = "123:ABC"
    config = Config(tmp_path / "c.json")
    config.update({"telegram": {"chat_id": 7}})
    http = ScriptedTG()
    bot = notify.TelegramBot(config, lambda t: "", lambda e, p: None, session=http)
    assert bot.send("абзац\n" * 2000)
    parts = [j["text"] for m, j in http.calls if m == "sendMessage"]
    assert len(parts) == 4 and all(len(p) <= notify.TG_LIMIT for p in parts)
    assert "".join(parts).count("абзац") == 2000


def test_bad_chat_id_in_config_does_not_crash(tmp_path, secrets):
    config = Config(tmp_path / "c.json")
    config.update({"telegram": {"chat_id": "мусор"}})
    bot = notify.TelegramBot(config, lambda t: "", lambda e, p: None, session=ScriptedTG())
    assert bot.chat_id() is None and bot.send("x") is False


class FakeSMTP:
    log: list = []

    def __init__(self, host, port, context=None, timeout=None):
        FakeSMTP.log.append(("open", type(self).__name__, host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        FakeSMTP.log.append(("ehlo",))

    def starttls(self, context=None):
        FakeSMTP.log.append(("starttls",))

    def login(self, user, password):
        FakeSMTP.log.append(("login", user))

    def send_message(self, m):
        FakeSMTP.log.append(("send", m["To"], [p.get_filename() for p in m.iter_attachments()]))


class FakeSSL(FakeSMTP):
    pass


def _mail_config(tmp_path, port):
    config = Config(tmp_path / "c.json")
    config.update({"email": {"enabled": True, "host": "smtp.corp.ru", "port": port, "login": "me@corp.ru", "to": "boss@corp.ru"}})
    return config


def test_mailer_587_uses_starttls(tmp_path, secrets, monkeypatch):
    FakeSMTP.log = []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSSL)
    secrets[notify.SMTP_KEY] = "p"
    notify.Mailer(_mail_config(tmp_path, "587")).send("Т", "текст", [("a.bin", b"1", "плохой-тип")])
    assert FakeSMTP.log[0] == ("open", "FakeSMTP", "smtp.corp.ru", 587)
    assert ("starttls",) in FakeSMTP.log and FakeSMTP.log[-1] == ("send", "boss@corp.ru", ["a.bin"])


def test_mailer_465_uses_ssl(tmp_path, secrets, monkeypatch):
    FakeSMTP.log = []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSSL)
    secrets[notify.SMTP_KEY] = "p"
    notify.Mailer(_mail_config(tmp_path, 465)).send("Т", "текст")
    assert FakeSMTP.log[0] == ("open", "FakeSSL", "smtp.corp.ru", 465) and ("starttls",) not in FakeSMTP.log


def test_mailer_errors_become_oserror(tmp_path, secrets, monkeypatch):
    class Broken(FakeSSL):
        def login(self, user, password):
            raise UnicodeEncodeError("ascii", "пароль", 0, 1, "bad")

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", Broken)
    secrets[notify.SMTP_KEY] = "пароль"
    with pytest.raises(OSError, match="Не удалось отправить"):
        notify.Mailer(_mail_config(tmp_path, 465)).send("Т", "текст")


def test_notifier_reports_mail_failure(tmp_path, secrets):
    config = _mail_config(tmp_path, 465)
    secrets[notify.SMTP_KEY] = "p"

    class BrokenMailer(notify.Mailer):
        def send(self, *a, **kw):
            raise OSError("Почта не пустила")

    errors = []
    bot = notify.TelegramBot(config, lambda t: "", lambda e, p: None, session=ScriptedTG())
    n = notify.Notifier(bot, BrokenMailer(config), config, on_error=errors.append)
    assert n.notify("Новые закупки", "текст") == []
    assert errors == ["Почта не пустила"]
