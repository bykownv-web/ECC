"""Бесплатный локальный ИИ через Ollama (https://ollama.com), модель работает на этом компьютере.

Используется родной API Ollama: POST /api/chat с инструментами (function calling).
"""

from __future__ import annotations

import json
from typing import Any, Callable

import requests

from .tools import TOOL_DEFINITIONS, ToolBox, ToolError

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:7b"
MAX_TOOL_ROUNDS = 6
MAX_HISTORY = 30  # локальной модели тяжело с длинной историей
REQUEST_TIMEOUT = 300  # первый запуск модели может грузиться минуту-две


class OllamaError(Exception):
    pass


def ollama_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOL_DEFINITIONS
    ]


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


class OllamaChat:
    def __init__(
        self,
        url: str,
        model: str,
        system_prompt: str,
        toolbox: ToolBox,
        on_tool: Callable[[str], None] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._url = (url or DEFAULT_URL).rstrip("/")
        self._model = model or DEFAULT_MODEL
        self._system = system_prompt
        self._toolbox = toolbox
        self._on_tool = on_tool
        self._http = session or requests.Session()
        self._messages: list[dict[str, Any]] = []

    def reset(self) -> None:
        self._messages = []

    def _chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            response = self._http.post(
                f"{self._url}/api/chat",
                json={
                    "model": self._model,
                    "messages": [{"role": "system", "content": self._system}, *messages],
                    "tools": ollama_tools(),
                    "stream": False,
                    "keep_alive": "30m",
                    "options": {"temperature": 0.4},
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.ConnectionError as exc:
            raise OllamaError("Ollama не запущена. Запустите приложение Ollama и повторите.") from exc
        except requests.Timeout as exc:
            raise OllamaError("Модель отвечает слишком долго. Попробуйте модель поменьше.") from exc
        except requests.RequestException as exc:
            raise OllamaError(f"Ошибка связи с Ollama ({exc.__class__.__name__}).") from exc
        if response.status_code == 404:
            raise OllamaError(
                f"Модель {self._model} не скачана. Выполните в PowerShell: ollama pull {self._model}"
            )
        if response.status_code == 400 and "does not support tools" in response.text:
            raise OllamaError(f"Модель {self._model} не умеет вызывать команды. Выберите qwen2.5 или llama3.1.")
        if response.status_code != 200:
            raise OllamaError(f"Ollama вернула ошибку {response.status_code}.")
        try:
            return response.json().get("message") or {}
        except ValueError as exc:
            raise OllamaError("Ollama вернула непонятный ответ.") from exc

    def ask(self, text: str) -> str:
        start = len(self._messages)
        self._messages.append({"role": "user", "content": text})
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                message = self._chat(self._messages)
                tool_calls = message.get("tool_calls") or []
                assistant = {"role": "assistant", "content": message.get("content") or ""}
                if tool_calls:
                    assistant["tool_calls"] = tool_calls
                self._messages.append(assistant)
                if not tool_calls:
                    self._trim()
                    return (message.get("content") or "").strip() or "Готово."
                for call in tool_calls:
                    fn = call.get("function") or {}
                    name = str(fn.get("name", ""))
                    if self._on_tool:
                        self._on_tool(name)
                    try:
                        result = self._toolbox.execute(name, _parse_arguments(fn.get("arguments")))
                    except ToolError as exc:
                        result = f"Ошибка: {exc}"
                    except Exception as exc:  # инструмент не должен ронять разговор
                        result = f"Внутренняя ошибка: {exc.__class__.__name__}"
                    self._messages.append({"role": "tool", "tool_name": name, "content": str(result)[:8000]})
            del self._messages[start:]
            return "Задача оказалась слишком длинной, я остановился. Уточните запрос."
        except OllamaError:
            del self._messages[start:]
            raise

    def _trim(self) -> None:
        if len(self._messages) <= MAX_HISTORY:
            return
        # Обрезаем по границе пользовательского сообщения, чтобы не разорвать вызов инструмента и его результат.
        cut = len(self._messages) - MAX_HISTORY
        while cut < len(self._messages) and self._messages[cut].get("role") != "user":
            cut += 1
        del self._messages[:cut]


def list_models(url: str = DEFAULT_URL) -> list[str]:
    try:
        response = requests.get(f"{(url or DEFAULT_URL).rstrip('/')}/api/tags", timeout=3)
        response.raise_for_status()
        return [m.get("name", "") for m in response.json().get("models", []) if m.get("name")]
    except (requests.RequestException, ValueError):
        return []
