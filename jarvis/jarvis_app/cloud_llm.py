"""Облачные модели: GigaChat (Сбер) и любые сервисы с OpenAI-совместимым API
(Hermes через Nous Portal или OpenRouter, YandexGPT, DeepSeek, OpenRouter, LM Studio и т.п.).

Оба клиента умеют две вещи: диалог с вызовом инструментов (как в окне Джарвиса) и ответ в JSON
(для разбора ТЗ, подбора и аналитики). Ключи хранятся в диспетчере учётных данных Windows.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable

import requests

from .certs import configure_session
from .notify import get_secret

OPENAI_KEY = "openai-compatible-api-key"
GIGACHAT_KEY = "gigachat-auth-key"
MAX_TOOL_ROUNDS = 6
MAX_HISTORY = 40

OPENAI_PRESETS = {
    "hermes_nous": ("https://inference-api.nousresearch.com/v1", "Hermes-4-70B"),
    "hermes_openrouter": ("https://openrouter.ai/api/v1", "nousresearch/hermes-4-70b"),
    "openrouter": ("https://openrouter.ai/api/v1", "deepseek/deepseek-chat-v3-0324:free"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "yandex": ("https://llm.api.cloud.yandex.net/v1", "gpt://<ID_каталога>/yandexgpt/latest"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "lmstudio": ("http://127.0.0.1:1234/v1", "local-model"),
}

GIGACHAT_OAUTH = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_API = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"


class CloudError(Exception):
    pass


def _tools_openai(tool_definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
        for t in tool_definitions
    ]


def _args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _status_error(provider: str, response: requests.Response) -> CloudError:
    code = response.status_code
    if code in (401, 403):
        return CloudError(f"{provider}: ключ не подошёл или нет доступа ({code}). Проверьте ключ в настройках.")
    if code == 402:
        return CloudError(f"{provider}: закончились средства или лимит на счёте.")
    if code == 404:
        return CloudError(f"{provider}: модель или адрес не найдены (404). Проверьте название модели.")
    if code == 429:
        return CloudError(f"{provider}: слишком много запросов, подождите минуту.")
    try:
        detail = response.json().get("error", {})
        detail = detail.get("message") if isinstance(detail, dict) else detail
    except ValueError:
        detail = ""
    return CloudError(f"{provider}: ошибка {code}. {detail or ''}".strip())


class _ToolChatMixin:
    """Общий цикл «модель → инструменты → модель» для OpenAI-подобных ответов."""

    name = "ИИ"

    def __init__(self, system_prompt: str, toolbox: Any, on_tool: Callable[[str], None] | None) -> None:
        self._system = system_prompt
        self._toolbox = toolbox
        self._on_tool = on_tool
        self._messages: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def reset(self) -> None:
        self._messages = []

    def _run_tool(self, name: str, args: dict[str, Any]) -> str:
        from .tools import ToolError

        if self._on_tool:
            self._on_tool(name)
        try:
            return self._toolbox.execute(name, args)[:8000]
        except ToolError as exc:
            return f"Ошибка: {exc}"
        except Exception as exc:  # инструмент не должен ронять разговор
            return f"Внутренняя ошибка: {exc.__class__.__name__}"

    def _trim(self) -> None:
        if len(self._messages) <= MAX_HISTORY:
            return
        cut = len(self._messages) - MAX_HISTORY
        while cut < len(self._messages) and self._messages[cut].get("role") != "user":
            cut += 1
        del self._messages[:cut]


class OpenAICompatChat(_ToolChatMixin):
    name = "Облачная модель"

    def __init__(self, base_url: str, model: str, system_prompt: str, toolbox: Any = None,
                 on_tool: Callable[[str], None] | None = None, session: requests.Session | None = None,
                 api_key: str | None = None) -> None:
        super().__init__(system_prompt, toolbox, on_tool)
        self._url = (base_url or "").rstrip("/")
        self._model = model
        self._http = session or requests.Session()
        self._key = api_key

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self._url or not self._model:
            raise CloudError("Не заданы адрес API и модель в настройках ИИ.")
        key = self._key if self._key is not None else get_secret(OPENAI_KEY)
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            response = self._http.post(f"{self._url}/chat/completions", json={"model": self._model, **body},
                                       headers=headers, timeout=180)
        except requests.ConnectionError as exc:
            raise CloudError(f"Нет связи с {self._url}. Проверьте интернет или адрес API.") from exc
        except requests.Timeout as exc:
            raise CloudError("Модель отвечает слишком долго.") from exc
        if response.status_code != 200:
            raise _status_error(self.name, response)
        try:
            data = response.json()
            return data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise CloudError("Сервис вернул непонятный ответ.") from exc

    def ask(self, text: str) -> str:
        with self._lock:
            start = len(self._messages)
            self._messages.append({"role": "user", "content": text})
            tools = _tools_openai(self._toolbox_defs())
            try:
                for _ in range(MAX_TOOL_ROUNDS):
                    msg = self._post({
                        "messages": [{"role": "system", "content": self._system}, *self._messages],
                        "tools": tools, "tool_choice": "auto", "temperature": 0.4,
                    })
                    calls = msg.get("tool_calls") or []
                    entry: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
                    if calls:
                        entry["tool_calls"] = calls
                    self._messages.append(entry)
                    if not calls:
                        self._trim()
                        return (msg.get("content") or "").strip() or "Готово."
                    for call in calls:
                        fn = call.get("function") or {}
                        result = self._run_tool(str(fn.get("name", "")), _args(fn.get("arguments")))
                        self._messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
                del self._messages[start:]
                return "Задача оказалась слишком длинной, я остановился. Уточните запрос."
            except CloudError:
                del self._messages[start:]
                raise

    @staticmethod
    def _toolbox_defs() -> list[dict[str, Any]]:
        from .tools import TOOL_DEFINITIONS

        return TOOL_DEFINITIONS

    def json(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        from .llm import _loads

        hint = "\n\nОтветь ТОЛЬКО одним JSON-объектом по этой схеме, без пояснений:\n" + json.dumps(schema, ensure_ascii=False)
        body = {"messages": [{"role": "system", "content": system + hint}, {"role": "user", "content": prompt}], "temperature": 0.1}
        try:
            msg = self._post({**body, "response_format": {"type": "json_object"}})
        except CloudError as exc:
            if "400" not in str(exc) and "422" not in str(exc):
                raise
            msg = self._post(body)  # сервис не поддерживает response_format
        return _loads(msg.get("content") or "")


class GigaChat(_ToolChatMixin):
    """GigaChat API (Сбер). Ключ авторизации → токен доступа на 30 минут. Нужен сертификат Минцифры (папка certs)."""

    name = "GigaChat"

    def __init__(self, model: str, system_prompt: str, toolbox: Any = None, on_tool: Callable[[str], None] | None = None,
                 scope: str = "GIGACHAT_API_PERS", session: requests.Session | None = None, auth_key: str | None = None) -> None:
        super().__init__(system_prompt, toolbox, on_tool)
        self._model = model or "GigaChat-2"
        self._scope = scope or "GIGACHAT_API_PERS"
        self._http = session or requests.Session()
        self._verify: bool | str = configure_session(self._http) if session is None else True
        self._auth_key = auth_key
        self._token = ""
        self._token_until = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_until - 60:
            return self._token
        key = self._auth_key if self._auth_key is not None else get_secret(GIGACHAT_KEY)
        if not key:
            raise CloudError("Не задан ключ авторизации GigaChat.")
        try:
            response = self._http.post(
                GIGACHAT_OAUTH,
                headers={"Authorization": f"Basic {key}", "RqUID": str(uuid.uuid4()),
                         "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                data={"scope": self._scope}, timeout=30, verify=self._verify,
            )
        except requests.exceptions.SSLError as exc:
            raise CloudError("GigaChat: не удалось проверить сертификат. Положите сертификаты Минцифры в папку certs (см. README).") from exc
        except requests.RequestException as exc:
            raise CloudError(f"GigaChat недоступен ({exc.__class__.__name__}).") from exc
        if response.status_code != 200:
            raise _status_error("GigaChat (авторизация)", response)
        data = response.json()
        self._token = data.get("access_token", "")
        expires = float(data.get("expires_at") or 0)
        self._token_until = expires / 1000 if expires > 1e11 else (expires or time.time() + 1500)
        return self._token

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._http.post(
                GIGACHAT_API, json={"model": self._model, **body},
                headers={"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"},
                timeout=180, verify=self._verify,
            )
        except requests.exceptions.SSLError as exc:
            raise CloudError("GigaChat: не удалось проверить сертификат. Положите сертификаты Минцифры в папку certs.") from exc
        except requests.RequestException as exc:
            raise CloudError(f"GigaChat недоступен ({exc.__class__.__name__}).") from exc
        if response.status_code == 401:
            self._token = ""
        if response.status_code != 200:
            raise _status_error("GigaChat", response)
        try:
            return response.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise CloudError("GigaChat вернул непонятный ответ.") from exc

    def ask(self, text: str) -> str:
        from .tools import TOOL_DEFINITIONS

        functions = [{"name": t["name"], "description": t["description"], "parameters": t["input_schema"]} for t in TOOL_DEFINITIONS]
        with self._lock:
            start = len(self._messages)
            self._messages.append({"role": "user", "content": text})
            try:
                for _ in range(MAX_TOOL_ROUNDS):
                    msg = self._post({
                        "messages": [{"role": "system", "content": self._system}, *self._messages],
                        "functions": functions, "function_call": "auto", "temperature": 0.4,
                    })
                    call = msg.get("function_call")
                    entry = {"role": "assistant", "content": msg.get("content") or ""}
                    if call:
                        entry["function_call"] = call
                        if msg.get("functions_state_id"):
                            entry["functions_state_id"] = msg["functions_state_id"]
                    self._messages.append(entry)
                    if not call:
                        self._trim()
                        return (msg.get("content") or "").strip() or "Готово."
                    name = str(call.get("name", ""))
                    result = self._run_tool(name, _args(call.get("arguments")))
                    self._messages.append({"role": "function", "name": name, "content": json.dumps({"result": result}, ensure_ascii=False)})
                del self._messages[start:]
                return "Задача оказалась слишком длинной, я остановился. Уточните запрос."
            except CloudError:
                del self._messages[start:]
                raise

    def json(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        from .llm import _loads

        hint = "\n\nОтветь ТОЛЬКО одним JSON-объектом по этой схеме, без пояснений и без markdown:\n" + json.dumps(schema, ensure_ascii=False)
        msg = self._post({"messages": [{"role": "system", "content": system + hint}, {"role": "user", "content": prompt}], "temperature": 0.1})
        return _loads(msg.get("content") or "")
