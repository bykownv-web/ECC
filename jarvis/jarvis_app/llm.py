"""Запрос к ИИ, который должен вернуть JSON по схеме: Ollama (локально) или Claude.

Используется для разбора ТЗ и подбора оборудования, где нужен структурированный ответ.
"""

from __future__ import annotations

import json
import re
from typing import Any

import requests

from .config import Config, get_api_key

FALLBACK_BETA = "server-side-fallback-2026-07-01"
OLLAMA_CTX = 16384


class LLMError(Exception):
    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw  # сырой ответ модели — для диагностики


def _close_truncated(text: str) -> str:
    """Дописывает закрывающие скобки и кавычку, если ответ оборвался на середине."""
    stack: list[str] = []
    in_str = escape = False
    for ch in text:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    tail = '"' if in_str else ""
    body = (text + tail).rstrip()
    body = re.sub(r",\s*$", "", body)          # висящая запятая
    body = re.sub(r':\s*$', ': ""', body)      # ключ без значения
    return body + "".join(reversed(stack))


def _repair(text: str) -> str:
    text = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u00ab", '"').replace("\u00bb", '"')
    text = re.sub(r"//[^\n\"]*\n", "\n", text)          # комментарии
    text = re.sub(r",\s*([}\]])", r"\1", text)            # запятая перед закрывающей скобкой
    text = re.sub(r"\bNone\b", "null", text)
    text = re.sub(r"\bTrue\b", "true", text)
    text = re.sub(r"\bFalse\b", "false", text)
    return text


def _loads(text: str) -> dict[str, Any]:
    raw = text or ""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)(```|$)", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start == -1:
        raise LLMError("ИИ вернул ответ не в формате JSON. Попробуйте ещё раз.", raw=raw)
    text = text[start:]
    end = text.rfind("}")
    attempts = []
    if end != -1:
        attempts += [text[: end + 1], _repair(text[: end + 1])]
    attempts += [_close_truncated(_repair(text)), _close_truncated(_repair(text[: end + 1] if end != -1 else text))]
    for candidate in attempts:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise LLMError("ИИ вернул повреждённый JSON. Попробуйте ещё раз или выберите модель мощнее.", raw=raw)


def schema_example(schema: dict[str, Any]) -> Any:
    """Короткий образец ответа по JSON-схеме — моделям его проще повторить, чем саму схему."""
    kind = schema.get("type")
    if kind == "object":
        return {k: schema_example(v) for k, v in (schema.get("properties") or {}).items()}
    if kind == "array":
        return [schema_example(schema.get("items") or {})]
    if "enum" in schema:
        return " | ".join(schema["enum"])
    return {"string": "...", "number": 0, "integer": 0, "boolean": False}.get(kind, "...")


def json_hint(schema: dict[str, Any]) -> str:
    return (
        "\n\nОтветь ТОЛЬКО одним JSON-объектом такой структуры (значения замени на свои, пустые поля оставь \"\" или []), "
        "без пояснений и без markdown:\n" + json.dumps(schema_example(schema), ensure_ascii=False)
    )


class LLM:
    def __init__(self, config: Config, session: requests.Session | None = None) -> None:
        self._config = config
        self._http = session or requests.Session()

    def provider(self) -> str:
        return str(self._config.get("ai_provider", "ollama"))

    @property
    def chunk_chars(self) -> int:
        """Размер куска ТЗ на один запрос: GigaChat и облачные модели надёжнее на кусках поменьше."""
        return {"gigachat": 6000, "openai": 9000}.get(self.provider(), 12000)

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
