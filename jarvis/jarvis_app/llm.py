"""Запрос к ИИ, который должен вернуть JSON по схеме: Ollama (локально), Claude или облачные модели.

Используется для разбора ТЗ и подбора оборудования, где нужен структурированный ответ.
Ответы моделей (особенно GigaChat) бывают «грязными»: JSON в тексте или в ```-блоке, висящие запятые,
«ёлочки» вместо кавычек, числа строками («1 234,5», «85%»), оборванный конец. _loads чинит синтаксис,
normalize приводит значения к типам из схемы и добавляет недостающие ключи, чтобы дальше не было KeyError.
"""

from __future__ import annotations

import ast
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


def _scan(text: str) -> tuple[list[str], bool]:
    """Незакрытые скобки и признак «оборвалось внутри строки»."""
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
    return stack, in_str


def _close_truncated(text: str) -> str:
    """Дописывает закрывающие скобки и кавычку, если ответ оборвался на середине."""
    stack, in_str = _scan(text)
    body = text
    if in_str:
        if body.endswith("\\") and not body.endswith("\\\\"):
            body = body[:-1]
        body += '"'
    body = body.rstrip()
    body = re.sub(r",\s*$", "", body)          # висящая запятая
    body = re.sub(r':\s*$', ': ""', body)      # ключ без значения
    return body + "".join(reversed(stack))


_OPEN_QUOTE = re.compile(r'([:\[,{]\s*)[«“„]')
_CLOSE_QUOTE = re.compile(r'[»”](\s*[,}\]:])')


def _repair(text: str) -> str:
    # «ёлочки» и “лапки” заменяем на кавычки только там, где они ограничивают строку JSON:
    # внутри значений («Поставка «Проектора»») они остаются как есть.
    text = _OPEN_QUOTE.sub(r'\1"', text)
    text = _CLOSE_QUOTE.sub(r'"\1', text)
    text = re.sub(r"(?m)^\s*//[^\n]*$", "", text)                   # строки-комментарии
    text = re.sub(r",\s*([}\]])", r"\1", text)                       # запятая перед закрывающей скобкой
    text = re.sub(r"\bNone\b", "null", text)
    text = re.sub(r"\bTrue\b", "true", text)
    text = re.sub(r"\bFalse\b", "false", text)
    text = re.sub(r'(:\s*)(-?\d[\d  ]*(?:[.,]\d+)?\s*%)(\s*[,}\]])', r'\1"\2"\3', text)  # 85% без кавычек
    text = re.sub(r"\}\s*\{", "},{", text)                            # пропущенная запятая между объектами
    return text


_DECODER = json.JSONDecoder(strict=False)  # strict=False: переводы строк внутри строк GigaChat не ломают разбор


def _wrap_list(data: list, schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """Модель вернула голый список вместо {"scores": [...]}: заворачиваем в единственное поле-массив схемы."""
    props = (schema or {}).get("properties") or {}
    arrays = [k for k, v in props.items() if isinstance(v, dict) and v.get("type") == "array"]
    if len(arrays) == 1:
        return {arrays[0]: data}
    if len(data) == 1 and isinstance(data[0], dict):
        return data[0]
    return None


def _accept(data: Any, schema: dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(data, dict) and data:
        return data
    if isinstance(data, list) and data:
        return _wrap_list(data, schema)
    return None


def _candidates(text: str) -> list[str]:
    """Варианты текста для разбора: из ```-блоков и целиком, каждый — как есть и починенный."""
    out = [m.group(1).strip() for m in re.finditer(r"```[ \t]*(?:json|JSON)?[ \t]*\n?(.*?)(?:```|$)", text, re.S)]
    out.append(text)
    result: list[str] = []
    for item in out:
        for variant in (item, _repair(item)):
            if variant and variant not in result:
                result.append(variant)
    return result


def _json_starts(text: str, limit: int = 40) -> list[int]:
    """Позиции «{» и «[», с которых похоже начинается JSON (а не фигурные скобки в пояснениях)."""
    out: list[int] = []
    for m in re.finditer(r"[{\[]", text):
        nxt = text[m.end():m.end() + 30].lstrip()[:1]
        if (m.group() == "{" and nxt in ('"', "}", "'")) or (m.group() == "[" and nxt and (nxt in '{["]-' or nxt.isdigit())):
            out.append(m.start())
            if len(out) >= limit:
                break
    return out


def _truncated(text: str, schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """Оборванный ответ: закрываем скобки; если не вышло — отрезаем хвост до запятой и пробуем снова."""
    stack, in_str = _scan(text)
    if not stack and not in_str:
        return None  # скобки сбалансированы — это не обрыв, а другая ошибка
    cuts = [len(text)] + [m.start() for m in re.finditer(r",", text)][::-1][:200]
    for cut in cuts:
        try:
            data = _DECODER.decode(_close_truncated(text[:cut]))
        except json.JSONDecodeError:
            continue
        got = _accept(data, schema)
        if got is not None:
            return got
    return None


def _loads(text: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = text or ""
    stripped = raw.strip()
    if "{" not in stripped and "[" not in stripped:
        raise LLMError("ИИ вернул ответ не в формате JSON. Попробуйте ещё раз.", raw=raw)
    candidates = _candidates(stripped)

    def parse_at(candidate: str, start: int) -> dict[str, Any] | None:
        try:
            data, _ = _DECODER.raw_decode(candidate, start)
        except json.JSONDecodeError:
            return None
        return _accept(data, schema)

    firsts = [(c, _json_starts(c, limit=1)) for c in candidates]
    for candidate, starts in firsts:  # 1) JSON с первой скобки (хвост с пояснениями после него не мешает)
        if starts and (got := parse_at(candidate, starts[0])) is not None:
            return got
    for candidate in dict.fromkeys(_repair(c) for c in candidates):  # 2) ответ оборвался на середине
        starts = _json_starts(candidate, limit=1)
        if starts and (got := _truncated(candidate[starts[0]:], schema)) is not None:
            return got
    for candidate in candidates:  # 3) перед JSON был текст со скобками — пробуем следующие «{»
        for start in _json_starts(candidate)[1:]:
            if (got := parse_at(candidate, start)) is not None:
                return got
    brace, end = stripped.find("{"), stripped.rfind("}")
    if brace != -1 and end > brace:  # 4) Python-словарь в одинарных кавычках: {'a': 'b'}
        try:
            data = ast.literal_eval(stripped[brace:end + 1])
            if isinstance(data, dict) and data:
                return json.loads(json.dumps(data, ensure_ascii=False, default=str))
        except (ValueError, SyntaxError, MemoryError, RecursionError, TypeError):
            pass
    raise LLMError("ИИ вернул повреждённый JSON. Попробуйте ещё раз или выберите модель мощнее.", raw=raw)


# ---------- приведение ответа к схеме ----------
_NUM_RE = re.compile(r"-?\d[\d\s  ']*(?:[.,]\d+)*")
_MULT = (("млрд", 1e9), ("млн", 1e6), ("тыс", 1e3))


def to_number(value: Any) -> float | None:
    """«1 234,5» → 1234.5; «85%» → 85; «≈ 450 тыс. руб.» → 450000; «100 000–150 000» → 100000. None — если числа нет."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value) if value == value else None  # NaN → None
    if not isinstance(value, str):
        return None
    text = value.replace("−", "-").replace("–", " – ").replace("—", " — ")
    match = _NUM_RE.search(text)
    if not match:
        return None
    num = re.sub(r"[\s  ']", "", match.group(0))
    if "," in num and "." in num:  # последний разделитель — дробный
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif num.count(",") > 1:
        num = num.replace(",", "")
    elif num.count(".") > 1:
        num = num.replace(".", "")
    else:
        num = num.replace(",", ".")
    try:
        result = float(num)
    except ValueError:
        return None
    tail = text[match.end():match.end() + 8].lower()
    for word, mult in _MULT:
        if tail.lstrip(" .").startswith(word):
            result *= mult
            break
    return result


_FALSE_WORDS = ("false", "нет", "no", "не треб", "не нуж", "отсутств", "не предусм", "0", "")
_TRUE_WORDS = ("true", "да", "yes", "треб", "нуж", "предусм", "1", "есть")


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None:
        return False
    text = str(value).strip().lower()
    if any(text.startswith(w) for w in _FALSE_WORDS if w) or not text:
        return False
    return any(text.startswith(w) for w in _TRUE_WORDS if w)


def _default(schema: dict[str, Any]) -> Any:
    kind = schema.get("type")
    if kind == "object":
        return {k: _default(v) for k, v in (schema.get("properties") or {}).items()}
    if kind == "array":
        return []
    if kind in ("number", "integer"):
        return 0
    if kind == "boolean":
        return False
    return ""


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "; ".join(s for s in (_to_str(v) for v in value) if s)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_to_str(v)}" for k, v in value.items() if _to_str(v))
    return str(value).strip()


def normalize(schema: dict[str, Any] | None, value: Any) -> Any:
    """Приводит ответ модели к схеме: числа из строк, строки из чисел и списков, недостающие ключи — по умолчанию."""
    if not isinstance(schema, dict) or not schema:
        return value
    kind = schema.get("type")
    if kind == "object":
        props = schema.get("properties") or {}
        if not props:
            return value if isinstance(value, dict) else {}
        if isinstance(value, list):
            value = _wrap_list(value, schema) or {}
        if not isinstance(value, dict):
            return _default(schema)
        lower = {str(k).strip().lower(): k for k in value}
        out: dict[str, Any] = {}
        for key, sub in props.items():
            src = key if key in value else lower.get(key.lower())
            out[key] = normalize(sub, value[src]) if src is not None else _default(sub)
        for key, val in value.items():  # лишние поля не мешают, но и не теряются
            if key not in out and str(key).strip().lower() not in {k.lower() for k in props}:
                out[key] = val
        return out
    if kind == "array":
        items = schema.get("items") or {}
        if value is None or value == "":
            return []
        if isinstance(value, dict) and items.get("type") != "object":
            value = list(value.values())
        if not isinstance(value, list):
            value = [value]
        if items.get("type") == "object":
            value = [v for v in value if isinstance(v, dict)]
        result = [normalize(items, v) for v in value]
        if items.get("type") == "string":
            result = [v for v in result if v]
        return result
    if kind in ("number", "integer"):
        num = to_number(value)
        if num is None:
            num = 0.0
        if kind == "integer":
            return int(round(num))
        return int(num) if num.is_integer() else num
    if kind == "boolean":
        return to_bool(value)
    if kind == "string":
        text = _to_str(value)
        enum = schema.get("enum")
        if enum:
            low = text.lower().strip(" .!«»\"")
            for option in enum:  # сначала точное совпадение, потом по началу (самые длинные варианты первыми)
                if low == option.lower():
                    return option
            for option in sorted(enum, key=len, reverse=True):
                if low.startswith(option.lower()):
                    return option
        return text
    return value


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
        """Ответ модели как JSON, приведённый к схеме (числа — числами, все обязательные ключи на месте)."""
        data = self._json_raw(system, prompt, schema)
        result = normalize(schema, data)
        return result if isinstance(result, dict) else {}

    def _json_raw(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
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
        except requests.RequestException as exc:
            raise LLMError(f"Ошибка связи с Ollama ({exc.__class__.__name__}).") from exc
        if response.status_code == 404:
            raise LLMError(f"Модель {model} не скачана: ollama pull {model}")
        if response.status_code != 200:
            raise LLMError(f"Ollama вернула ошибку {response.status_code}.")
        try:
            content = (response.json().get("message") or {}).get("content", "")
        except ValueError as exc:
            raise LLMError("Ollama вернула непонятный ответ.") from exc
        return _loads(content, schema)

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
        return _loads(text, schema)
