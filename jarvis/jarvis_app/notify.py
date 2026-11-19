"""Уведомления и удалённый доступ: Telegram-бот и почта (SMTP).

Telegram: бот отвечает только одному чату — тому, кто прислал /start с кодом привязки из окна Джарвиса.
Почта: отправка отчётов через SMTP по паролю приложения (Яндекс, Mail.ru, Gmail или свой сервер).
Токен бота и пароль почты хранятся в диспетчере учётных данных Windows.
"""

from __future__ import annotations

import random
import re
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formatdate
from typing import Any, Callable

import requests

from .config import Config

KEYRING_SERVICE = "jarvis-assistant"
TELEGRAM_KEY = "telegram-bot-token"
SMTP_KEY = "smtp-password"
TG_API = "https://api.telegram.org/bot{token}/{method}"
TG_LIMIT = 4000

SMTP_PRESETS = {
    "yandex": ("smtp.yandex.ru", 465),
    "mailru": ("smtp.mail.ru", 465),
    "gmail": ("smtp.gmail.com", 465),
}

# Действия, которые нельзя выполнять удалённо из Telegram
REMOTE_BLOCKED_TOOLS = {"power"}


def get_secret(name: str) -> str:
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, name) or ""
    except Exception:
        return ""


def set_secret(name: str, value: str) -> bool:
    try:
        import keyring

        if value:
            keyring.set_password(KEYRING_SERVICE, name, value)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, name)
            except Exception:
                pass
        return True
    except Exception:
        return False


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    text = text or ""
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts or [""]


class TelegramError(Exception):
    pass


class TelegramBot:
    """Длинный опрос getUpdates в фоновом потоке."""

    def __init__(
        self,
        config: Config,
        on_message: Callable[[str], str],
        on_event: Callable[[str, dict[str, Any]], None],
        session: requests.Session | None = None,
    ) -> None:
        self._config = config
        self._on_message = on_message
        self._on_event = on_event
        self._http = session or requests.Session()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.bot_name = ""
        self.last_ok = 0.0
        self.last_error = ""
        self.started_at = time.time()

    # Код привязки хранится в настройках до привязки, чтобы пережить перезапуск Джарвиса.
    @property
    def pair_code(self) -> str:
        return str(self._config.get("telegram", {}).get("pair_code") or "")

    @pair_code.setter
    def pair_code(self, value: str) -> None:
        self._config.update({"telegram": {"pair_code": value}})

    @staticmethod
    def _norm_code(code: str) -> str:
        return re.sub(r"[^0-9A-Z]", "", (code or "").upper())

    # ---------- API ----------
    def _call(self, method: str, token: str | None = None, http_timeout: float = 20, **params) -> Any:
        token = token or get_secret(TELEGRAM_KEY)
        if not token:
            raise TelegramError("Не задан токен бота.")
        try:
            response = self._http.post(TG_API.format(token=token, method=method), json=params, timeout=http_timeout)
        except requests.RequestException as exc:
            raise TelegramError(f"Telegram недоступен ({exc.__class__.__name__}).") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramError(f"Telegram вернул ошибку {response.status_code}.") from exc
        if response.status_code == 409 or data.get("error_code") == 409:
            raise TelegramError(
                "бот уже опрашивается в другом месте: закройте второе окно Джарвиса "
                "(или другую программу с этим токеном)."
            )
        if response.status_code == 401 or data.get("error_code") == 401:
            raise TelegramError("Токен бота не подошёл. Скопируйте его из @BotFather ещё раз.")
        if not data.get("ok"):
            raise TelegramError(str(data.get("description") or f"ошибка {response.status_code}"))
        return data.get("result")

    def chat_id(self) -> int | None:
        value = self._config.get("telegram", {}).get("chat_id")
        return int(value) if value else None

    def send(self, text: str, chat_id: int | None = None) -> bool:
        chat_id = chat_id or self.chat_id()
        if not chat_id:
            return False
        try:
            for part in split_message(text):
                self._call("sendMessage", chat_id=chat_id, text=part, disable_web_page_preview=True)
            return True
        except TelegramError as exc:
            self._on_event("error", {"text": f"Telegram: {exc}"})
            return False

    # ---------- подключение ----------
    def connect(self, token: str) -> dict[str, Any]:
        token = (token or "").strip()
        if token and ":" not in token:
            raise TelegramError("Токен выглядит как 1234567890:AAH… — скопируйте его целиком.")
        if token:
            me = self._call("getMe", token=token)
            if not set_secret(TELEGRAM_KEY, token):
                raise TelegramError("Не удалось сохранить токен в диспетчере учётных данных Windows.")
            self.bot_name = me.get("username", "")
        if not self.pair_code or self.chat_id() is None:
            self.pair_code = f"J-{random.SystemRandom().randint(1000, 9999)}"
        self._config.update({"telegram": {"enabled": True, "bot_name": self.bot_name or self._config.get("telegram", {}).get("bot_name", "")}})
        self.start()
        return {"bot": self.bot_name, "code": self.pair_code}

    def disconnect(self) -> None:
        self.stop()
        set_secret(TELEGRAM_KEY, "")
        self._config.update({"telegram": {"enabled": False, "chat_id": None, "bot_name": ""}})

    def status(self) -> dict[str, Any]:
        tg = self._config.get("telegram", {}) or {}
        bot = tg.get("bot_name", "")
        code = self.pair_code
        return {
            "enabled": bool(tg.get("enabled")) and bool(get_secret(TELEGRAM_KEY)),
            "paired": bool(tg.get("chat_id")),
            "bot": bot,
            "code": code,
            "link": f"https://t.me/{bot}?start={self._norm_code(code)}" if bot and code else "",
            "running": self._thread is not None and self._thread.is_alive(),
            "last_ok": time.strftime("%H:%M:%S", time.localtime(self.last_ok)) if self.last_ok else "",
            "last_error": self.last_error,
        }

    # ---------- цикл ----------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not get_secret(TELEGRAM_KEY):
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(self._stop,), daemon=True, name="jarvis-telegram")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self, stop: threading.Event) -> None:
        offset = None
        backoff = 5
        try:  # если у бота когда-то был webhook, getUpdates не работает — снимаем его
            self._call("deleteWebhook", drop_pending_updates=False)
        except TelegramError:
            pass
        while not stop.is_set():
            try:
                offset = self.poll_once(offset)
                backoff = 5
            except TelegramError as exc:
                if str(exc) != self.last_error:
                    self._on_event("telegram_status", {"text": f"Telegram: {exc}", "error": True})
                self.last_error = str(exc)
                stop.wait(backoff)
                backoff = min(backoff * 2, 300)
            except Exception as exc:  # не глотаем молча: показываем в настройках
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                stop.wait(backoff)
                backoff = min(backoff * 2, 300)

    def poll_once(self, offset: int | None) -> int | None:
        """Один длинный опрос getUpdates: обрабатывает сообщения и возвращает новый offset."""
        params: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        updates = self._call("getUpdates", http_timeout=35, **params) or []
        self.last_ok = time.time()
        self.last_error = ""
        for update in updates:
            offset = update["update_id"] + 1
            try:
                self.handle_update(update)
            except TelegramError as exc:
                self.last_error = str(exc)
        return offset

    def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        if message.get("date") and message["date"] < self.started_at - 120:
            return  # старые сообщения, накопившиеся пока Джарвис был выключен, не обрабатываем
        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not text or chat_id is None or chat.get("type") != "private":
            return
        paired = self.chat_id()
        if text.startswith("/start"):
            code = text[len("/start"):].strip().upper()
            if paired == chat_id:
                self.send("Я на связи, сэр. Пишите команды или вопросы.", chat_id)
            elif self.pair_code and self._norm_code(code) == self._norm_code(self.pair_code):
                self._config.update({"telegram": {"chat_id": chat_id}})
                self.pair_code = ""
                name = (message.get("from") or {}).get("first_name", "")
                self.send(
                    "Привязка выполнена. Теперь я отвечаю только вам.\n"
                    "Команды: /zakupki — свежие закупки, /analytics — аналитика, /help — помощь. "
                    "Или просто пишите, как в окне Джарвиса.",
                    chat_id,
                )
                self._on_event("telegram_status", {"text": f"Telegram привязан: {name}", "error": False, "paired": True})
            else:
                hint = "Этот бот приватный. Отправьте /start и код привязки из окна Джарвиса (Настройки → Telegram)."
                if not code:
                    hint += " Проще всего нажать в Джарвисе кнопку «Открыть бота и привязать»."
                self._call("sendMessage", chat_id=chat_id, text=hint)
            return
        if paired != chat_id:
            return  # чужие сообщения игнорируем молча
        if text == "/help":
            self.send(
                "Я понимаю обычные фразы: «найди закупки проектор», «какая погода», «напомни через 10 минут…».\n"
                "/zakupki — поиск закупок по вашим тегам\n/analytics — последние отчёты аналитики",
                chat_id,
            )
            return
        try:
            self._call("sendChatAction", chat_id=chat_id, action="typing")
        except TelegramError:
            pass
        reply = self._on_message(text)
        self.send(reply or "Готово.", chat_id)


class Mailer:
    def __init__(self, config: Config) -> None:
        self._config = config

    def settings(self) -> dict[str, Any]:
        return self._config.get("email", {}) or {}

    def configured(self) -> bool:
        s = self.settings()
        return bool(s.get("enabled") and s.get("host") and s.get("login") and s.get("to") and get_secret(SMTP_KEY))

    def send(self, subject: str, body: str, attachments: list[tuple[str, bytes, str]] | None = None) -> None:
        s = self.settings()
        password = get_secret(SMTP_KEY)
        if not (s.get("host") and s.get("login") and password and s.get("to")):
            raise OSError("Почта не настроена: нужны сервер, логин, пароль приложения и адрес получателя.")
        msg = EmailMessage()
        msg["From"] = s["login"]
        msg["To"] = s["to"]
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg.set_content(body)
        for name, data, mime in attachments or []:
            maintype, subtype = mime.split("/", 1)
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
        port = int(s.get("port") or 465)
        context = ssl.create_default_context()
        try:
            if port == 465:
                with smtplib.SMTP_SSL(s["host"], port, context=context, timeout=30) as smtp:
                    smtp.login(s["login"], password)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(s["host"], port, timeout=30) as smtp:
                    smtp.starttls(context=context)
                    smtp.login(s["login"], password)
                    smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError as exc:
            raise OSError("Почта не пустила: проверьте логин и пароль приложения (обычный пароль не подойдёт).") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise OSError(f"Не удалось отправить письмо: {exc}") from exc


class Notifier:
    """Один вход для уведомлений: окно, Telegram и почта."""

    def __init__(self, bot: TelegramBot, mailer: Mailer, config: Config) -> None:
        self.bot = bot
        self.mailer = mailer
        self._config = config
        self._last_mail = 0.0

    def notify(self, title: str, text: str, email: bool = True) -> list[str]:
        sent = []
        if self._config.get("telegram", {}).get("notify", True) and self.bot.chat_id():
            if self.bot.send(f"🔔 {title}\n\n{text}"):
                sent.append("telegram")
        if email and self.mailer.configured() and self._config.get("email", {}).get("notify", True):
            try:
                self.mailer.send(f"J.A.R.V.I.S.: {title}", text)
                sent.append("email")
                self._last_mail = time.time()
            except OSError:
                pass
        return sent
