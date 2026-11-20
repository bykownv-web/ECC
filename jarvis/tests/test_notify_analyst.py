import pytest

from jarvis_app import analyst as an
from jarvis_app import notify
from jarvis_app.config import Config
from jarvis_app.procurement import Purchase


@pytest.fixture
def secrets(monkeypatch):
    store = {}
    monkeypatch.setattr(notify, "get_secret", lambda name: store.get(name, ""))
    monkeypatch.setattr(notify, "set_secret", lambda name, value: store.__setitem__(name, value) or True)
    return store


class FakeTG:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        self.calls.append((method, json))

        class R:
            status_code = 200

            def json(self_inner):
                if method == "getMe":
                    return {"ok": True, "result": {"username": "test_bot"}}
                return {"ok": True, "result": True}

        return R()


def msg(chat_id, text):
    return {"update_id": 1, "message": {"text": text, "chat": {"id": chat_id, "type": "private"}, "from": {"first_name": "Н"}}}


def test_telegram_pairing_and_privacy(tmp_path, secrets):
    config = Config(tmp_path / "c.json")
    http = FakeTG()
    asked = []
    bot = notify.TelegramBot(config, lambda t: asked.append(t) or "ответ", lambda e, p: None, session=http)
    bot.start = lambda: None  # без фонового потока в тесте
    info = bot.connect("123:ABC")
    assert info["bot"] == "test_bot" and info["code"].startswith("J-")
    assert secrets[notify.TELEGRAM_KEY] == "123:ABC"

    bot.handle_update(msg(555, "привет"))  # до привязки — игнор
    bot.handle_update(msg(555, "/start J-0000"))  # неверный код
    assert config.get("telegram")["chat_id"] is None
    bot.handle_update(msg(777, f"/start {info['code']}"))
    assert config.get("telegram")["chat_id"] == 777
    bot.handle_update(msg(555, "который час"))  # чужой чат после привязки
    assert asked == []
    bot.handle_update(msg(777, "который час"))
    assert asked == ["который час"]
    sent = [j["text"] for m, j in http.calls if m == "sendMessage" and j["chat_id"] == 777]
    assert sent[-1] == "ответ"


def test_telegram_rejects_bad_token(tmp_path, secrets):
    bot = notify.TelegramBot(Config(tmp_path / "c.json"), lambda t: "", lambda e, p: None, session=FakeTG())
    with pytest.raises(notify.TelegramError):
        bot.connect("без двоеточия")


def test_split_message():
    parts = notify.split_message("строка\n" * 1500, limit=4000)
    assert all(len(p) <= 4000 for p in parts) and "".join(parts).count("строка") == 1500


def test_remote_toolbox_blocks_power():
    from jarvis_app.api import RemoteToolBox
    from jarvis_app.tools import ToolError

    class TB:
        def execute(self, name, args):
            return "ok"

        def last_results(self):
            return []

    remote = RemoteToolBox(TB())
    assert remote.execute("get_time", {}) == "ok"
    assert remote.last_results() == []
    with pytest.raises(ToolError):
        remote.execute("power", {"action": "shutdown"})


def test_mailer_sends_via_ssl(tmp_path, secrets, monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, context=None, timeout=None):
            sent["host"], sent["port"] = host, port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, m):
            sent["subject"] = m["Subject"]
            sent["to"] = m["To"]

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
    config = Config(tmp_path / "c.json")
    config.update({"email": {"enabled": True, "host": "smtp.yandex.ru", "port": 465, "login": "me@ya.ru", "to": "boss@ya.ru"}})
    secrets[notify.SMTP_KEY] = "app-pass"
    mailer = notify.Mailer(config)
    assert mailer.configured()
    mailer.send("Тема", "Текст")
    assert sent == {"host": "smtp.yandex.ru", "port": 465, "login": ("me@ya.ru", "app-pass"), "subject": "Тема", "to": "boss@ya.ru"}


def test_compute_totals_and_margin():
    est = {
        "equipment": [{"quantity": 2, "unit_cost_min": 100, "unit_cost_max": 150}, {"quantity": 0, "unit_cost_min": 50, "unit_cost_max": 40}],
        "installation_required": True, "installation_cost_min": 30, "installation_cost_max": 60,
    }
    t = an.compute_totals(est, 1000.0)
    assert (t["equipment_min"], t["equipment_max"]) == (240.0, 350.0)
    assert (t["cost_min"], t["cost_max"]) == (270.0, 410.0)
    assert (t["margin_min"], t["margin_max"], t["margin_pct_min"]) == (590.0, 730.0, 59.0)
    est["installation_required"] = False
    assert an.compute_totals(est, None)["installation_max"] == 0 and an.compute_totals(est, None)["margin_min"] is None


class FakeLLM:
    def __init__(self, handler):
        self.handler = handler
        self.calls = 0

    def json(self, system, prompt, schema):
        self.calls += 1
        return self.handler(system, prompt)


ESTIMATE = {
    "equipment": [{"name": "Проектор", "quantity": 2, "unit": "шт", "key_specs": "6000 лм", "unit_cost_min": 400000, "unit_cost_max": 450000, "price_basis": "оценка ИИ"}],
    "installation_required": True, "installation_scope": "монтаж", "installation_cost_min": 100000, "installation_cost_max": 150000,
    "other_costs": "", "risks": ["ПП 1875"], "recommendation": "участвовать", "reasoning": "профиль",
}
ANALYSIS = {"summary": "", "positions": [{"name": "Проектор", "quantity": "2", "unit": "шт", "okpd2": "", "characteristics": []}]}


def test_analyst_scores_and_reports_top(tmp_path, monkeypatch):
    monkeypatch.setattr(an, "documents_dir", lambda: tmp_path / "docs")
    config = Config(tmp_path / "c.json")
    config.update({"analyst": {"profile": "проекторы и звук", "min_score": 60, "top_n": 1}})

    def handler(system, prompt):
        if system == an.SCORE_SYSTEM:
            return {"scores": [{"number": "A", "score": 95, "reason": "проекторы"}, {"number": "B", "score": 70, "reason": "звук"},
                               {"number": "C", "score": 10, "reason": "дороги"}]}
        if system == an.ESTIMATE_SYSTEM:
            return ESTIMATE
        raise AssertionError(system)

    llm = FakeLLM(handler)
    monkeypatch.setattr(an.tender_docs, "fetch_eis_documents", lambda client, url, dest: [])
    monkeypatch.setattr(an.tender_docs, "process_files", lambda files, work: ([], "ТЗ: поставка проекторов 2 шт"))
    monkeypatch.setattr(an.tender_ai, "analyze_tz", lambda llm_, text, on_progress=None: ANALYSIS)
    notes = []
    analyst = an.Analyst(config, lambda: None, lambda e, p: None, notify=lambda t, x: notes.append((t, x)),
                         store=an.AnalystStore(tmp_path / "a.json"), llm_factory=lambda: llm)
    purchases = [Purchase(number=n, url=f"https://zakupki.gov.ru/{n}", title=f"Закупка {n}", price=2_000_000.0) for n in "ABC"]
    reports = analyst.run(purchases)
    assert [r["purchase"]["number"] for r in reports] == ["A"]  # top_n=1, лучшая по баллам
    r = reports[0]
    assert r["totals"]["cost_min"] == 900000 and r["totals"]["margin_max"] == 1_100_000
    assert notes and "95/100" in notes[0][0] and "Маржа к НМЦК" in notes[0][1]
    calls_before = llm.calls
    analyst.run(purchases)
    # повторный запуск: уже оценённые не оцениваются заново, B разбирается следующей
    second = analyst.store.reports[0]["purchase"]["number"]
    assert second == "B" and llm.calls == calls_before + 1  # только оценка стоимости B


def test_analyst_requires_profile(tmp_path):
    config = Config(tmp_path / "c.json")
    config.update({"analyst": {"profile": ""}})
    analyst = an.Analyst(config, lambda: None, lambda e, p: None, store=an.AnalystStore(tmp_path / "a.json"), llm_factory=lambda: None)
    with pytest.raises(ValueError, match="профиль"):
        analyst.run([Purchase(number="1", url="u")])


def test_failed_deep_report_is_kept_with_error(tmp_path, monkeypatch):
    monkeypatch.setattr(an, "documents_dir", lambda: tmp_path / "docs")
    from jarvis_app.procurement import ProcurementError

    def boom(client, url, dest):
        raise ProcurementError("нет файлов")

    monkeypatch.setattr(an.tender_docs, "fetch_eis_documents", boom)
    config = Config(tmp_path / "c.json")
    analyst = an.Analyst(config, lambda: None, lambda e, p: None, store=an.AnalystStore(tmp_path / "a.json"), llm_factory=lambda: None)
    report = analyst.deep_report({"number": "X", "url": "u", "title": "T", "score": 80, "reason": "r", "price": 1.0})
    assert report["error"] == "нет файлов"
    assert "Глубокий разбор не удался" in an.format_report(report)


def test_pair_code_survives_restart_and_deep_link_format(tmp_path, secrets):
    config = Config(tmp_path / "c.json")
    bot = notify.TelegramBot(config, lambda t: "ok", lambda e, p: None, session=FakeTG())
    bot.start = lambda: None
    code = bot.connect("123:ABC")["code"]
    st = bot.status()
    assert st["link"] == f"https://t.me/test_bot?start={code.replace('-', '')}"
    # перезапуск приложения: новый объект бота, тот же конфиг — код прежний
    bot2 = notify.TelegramBot(Config(tmp_path / "c.json"), lambda t: "ok", lambda e, p: None, session=FakeTG())
    assert bot2.pair_code == code
    # «Старт» по ссылке присылает код без дефиса
    bot2.handle_update(msg(42, "/start " + code.replace("-", "").lower()))
    assert bot2.chat_id() == 42 and bot2.pair_code == ""


class PollingTG(FakeTG):
    """Отдаёт getUpdates как настоящий Telegram и проверяет параметры запроса."""

    def __init__(self, updates):
        super().__init__()
        self.updates = updates

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        if method == "getUpdates":
            assert json["timeout"] == 25 and timeout == 35  # таймаут Telegram и HTTP — разные параметры
            self.calls.append((method, json))
            batch, self.updates = self.updates, []

            class R:
                status_code = 200

                def json(self_inner):
                    return {"ok": True, "result": batch}

            return R()
        return super().post(url, json=json, timeout=timeout)


def test_poll_once_receives_start_and_pairs(tmp_path, secrets):
    config = Config(tmp_path / "c.json")
    secrets[notify.TELEGRAM_KEY] = "123:ABC"
    config.update({"telegram": {"enabled": True, "bot_name": "test_bot", "pair_code": "J-4965"}})
    http = PollingTG([dict(msg(7, "/start"), update_id=10), dict(msg(7, "/start J-4965"), update_id=11)])
    bot = notify.TelegramBot(config, lambda t: "ok", lambda e, p: None, session=http)
    offset = bot.poll_once(None)
    assert offset == 12 and bot.chat_id() == 7 and bot.last_ok and bot.last_error == ""
    replies = [j["text"] for m, j in http.calls if m == "sendMessage"]
    assert "приватный" in replies[0] and "Привязка выполнена" in replies[1]


def test_old_backlog_messages_are_ignored(tmp_path, secrets):
    import time as _t

    config = Config(tmp_path / "c.json")
    config.update({"telegram": {"pair_code": "J-1111"}})
    http = FakeTG()
    bot = notify.TelegramBot(config, lambda t: "ok", lambda e, p: None, session=http)
    old = msg(5, "/start J-1111")
    old["message"]["date"] = int(_t.time()) - 3600
    bot.handle_update(old)
    assert bot.chat_id() is None and http.calls == []


def test_compute_totals_tolerates_strings_and_none():
    est = {"equipment": [{"quantity": "2 шт", "unit_cost_min": "100 000,00", "unit_cost_max": None}, "мусор"],
           "installation_required": "да", "installation_cost_min": "15 тыс.", "installation_cost_max": None}
    t = an.compute_totals(est, "218 667,00")
    assert (t["equipment_min"], t["equipment_max"]) == (200000.0, 200000.0)
    assert (t["installation_min"], t["installation_max"], t["nmck"]) == (15000.0, 15000.0, 218667.0)
    assert an.compute_totals({"installation_required": "false", "installation_cost_min": 5}, None)["installation_min"] == 0


def test_analyst_busy_is_a_value_error(tmp_path):
    config = Config(tmp_path / "c.json")
    analyst = an.Analyst(config, lambda: None, lambda e, p: None, store=an.AnalystStore(tmp_path / "a.json"), llm_factory=lambda: None)
    analyst._lock.acquire()
    with pytest.raises(an.AnalystBusy, match="уже выполняется"):
        analyst.run([])
    assert issubclass(an.AnalystBusy, ValueError)
