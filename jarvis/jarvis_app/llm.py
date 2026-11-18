"""Запрос к ИИ, который должен вернуть JSON по схеме: Ollama (локально) или Claude.

Используется для разбора ТЗ и подбора оборудования, где нужен структурированный ответ.
"""

from __future__ import annotations

import json
from typing import Any

import requests

from .config import Config, get_api_key

FALLBACK_BETA = "server-side-fallback-2026-07-01"
OLLAMA_CTX = 16384


class LLMError(Exception):
    pass


def _loads(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise LLMError("ИИ вернул ответ не в формате JSON. Попробуйте ещё раз.")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError("ИИ вернул повреждённый JSON. Попробуйте ещё раз или выберите модель мощнее.") from exc
    if not isinstance(data, dict):
        raise LLMError("ИИ вернул неожиданный ответ.")
    return data


class LLM:
    def __init__(self, config: Config, session: requests.Session | None = None) -> None:
        self._config = config
        self._http = session or requests.Session()

    def provider(self) -> str:
        return str(self._config.get("ai_provider", "ollama"))

    def json(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        provider = self.provider()
        if provider == "ollama":
            return self._ollama(system, prompt, schema)
        if provider == "claude":
            return self._claude(system, prompt, schema)
        if provider in ("openai", "gigachat"):
            from .cloud_llm import CloudError, GigaChat, OpenAICompatChat

            try:
                if provider == "openai":
                    client = OpenAICompatChat(str(self._config.get("openai_base_url", "")), str(self._config.get("openai_model", "")), "")
                else:
                    client = GigaChat(str(self._config.get("gigachat_model", "")), "", scope=str(self._config.get("gigachat_scope", "")))
                return client.json(system, prompt, schema)
            except CloudError as exc:
                raise LLMError(str(exc)) from exc
        raise LLMError("Для этой функции нужен ИИ. Включите Ollama или Claude в настройках.")

    def _ollama(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        url = str(self._config.get("ollama_url", "http://127.0.0.1:11434")).rstrip("/")
        model = str(self._config.get("ollama_model", "qwen2.5:7b"))
        try:
            response = self._http.post(
                f"{url}/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                    "format": schema,
                    "stream": False,
                    "keep_alive": "30m",
                    "options": {"temperature": 0.1, "num_ctx": OLLAMA_CTX},
                },
                timeout=900,
            )
        except requests.ConnectionError as exc:
            raise LLMError("Ollama не запущена. Запустите приложение Ollama.") from exc
        except requests.Timeout as exc:
            raise LLMError("Модель не успела ответить за 15 минут. Попробуйте документ поменьше.") from exc
        if response.status_code == 404:
            raise LLMError(f"Модель {model} не скачана: ollama pull {model}")
        if response.status_code != 200:
            raise LLMError(f"Ollama вернула ошибку {response.status_code}.")
        try:
            content = (response.json().get("message") or {}).get("content", "")
        except ValueError as exc:
            raise LLMError("Ollama вернула непонятный ответ.") from exc
        return _loads(content)

    def _claude(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        key = get_api_key()
        if not key:
            raise LLMError("Не задан API-ключ Claude.")
        import anthropic

        client = anthropic.Anthropic(api_key=key, max_retries=2, timeout=600)
        try:
            with client.beta.messages.stream(
                model=self._config.get("model", "claude-opus-5-5"),
                max_tokens=64000,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "effort": self._config.get("effort", "low"),
                    "format": {"type": "json_schema", "schema": schema},
                },
                betas=[FALLBACK_BETA],
                fallbacks="default",
            ) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise LLMError("API-ключ Claude не подошёл.") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Слишком много запросов к Claude, попробуйте через минуту.") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"Claude отклонил запрос: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Ошибка сервера Claude ({exc.status_code}).") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("Нет связи с Claude.") from exc
        if message.stop_reason == "refusal":
            raise LLMError("Claude отказался обрабатывать этот документ.")
        if message.stop_reason == "max_tokens":
            raise LLMError("Документ слишком большой для одного ответа.")
        text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
        return _loads(text)
