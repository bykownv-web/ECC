"""«Мозг» Джарвиса: Claude или бесплатная локальная модель Ollama с инструментами, а без ИИ — простые команды."""

from __future__ import annotations

import threading
from typing import Any, Callable

from .config import Config, get_api_key
from .local_commands import parse_command
from .cloud_llm import CloudError, GigaChat, OpenAICompatChat
from .ollama_brain import OllamaChat, OllamaError
from .procurement import analyze, format_money
from .tools import TOOL_DEFINITIONS, ToolBox, ToolError

MAX_TOOL_ROUNDS = 8
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """Ты — {name}, голосовой помощник на компьютере пользователя под Windows, в духе J.A.R.V.I.S.
Обращайся к пользователю «{user}». Отвечай по-русски, коротко и по делу: ответ часто зачитывается вслух,
поэтому без таблиц, markdown-разметки и длинных списков — не больше 3–5 предложений, если не просят подробнее.

Ты умеешь управлять компьютером и искать закупки по 44-ФЗ и 223-ФЗ через инструменты. Пользователь работает
с закупками проекторов, мультимедийного и звукового оборудования и оборудования для театров.
Когда просят найти закупки — вызывай search_procurements; когда просят анализ — опирайся на найденные данные:
называй количество, общую сумму НМЦК, крупнейшие закупки и заказчиков, близкие сроки подачи заявок.
Не придумывай закупки, номера и суммы — бери их только из результатов инструментов.
Выключай или перезагружай компьютер только по прямой просьбе пользователя.
Если действие не удалось, честно скажи об этом и предложи, что сделать."""


class Brain:
    def __init__(self, config: Config, toolbox: ToolBox, on_tool: Callable[[str], None] | None = None) -> None:
        self._config = config
        self._toolbox = toolbox
        self._on_tool = on_tool
        self._messages: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._client = None
        self._client_key = ""
        self._ollama = None  # текущий чат: Ollama, GigaChat или OpenAI-совместимый
        self._ollama_sig: tuple[str, ...] | None = None

    def reset(self) -> None:
        with self._lock:
            self._messages = []
            if self._ollama is not None:
                self._ollama.reset()

    def provider(self) -> str:
        return str(self._config.get("ai_provider", "ollama"))

    def has_ai(self) -> bool:
        provider = self.provider()
        if provider in ("ollama", "openai", "gigachat"):
            return True
        return provider == "claude" and bool(get_api_key())

    def _system_prompt(self) -> str:
        return SYSTEM_PROMPT.format(
            name=self._config.get("assistant_name", "Джарвис"),
            user=self._config.get("user_name", "сэр"),
        )

    def _get_cloud(self):
        provider = self.provider()
        if provider == "openai":
            sig = ("openai", str(self._config.get("openai_base_url", "")), str(self._config.get("openai_model", "")), self._system_prompt())
            if self._ollama_sig != sig:
                self._ollama = OpenAICompatChat(sig[1], sig[2], sig[3], self._toolbox, on_tool=self._on_tool)
                self._ollama_sig = sig
        else:
            sig = ("gigachat", str(self._config.get("gigachat_model", "")), str(self._config.get("gigachat_scope", "")), self._system_prompt())
            if self._ollama_sig != sig:
                self._ollama = GigaChat(sig[1], sig[3], self._toolbox, on_tool=self._on_tool, scope=sig[2])
                self._ollama_sig = sig
        return self._ollama

    def _get_ollama(self) -> OllamaChat:
        sig = (
            "ollama",
            str(self._config.get("ollama_url", "")),
            str(self._config.get("ollama_model", "")),
            self._system_prompt(),
        )
        if self._ollama is None or sig != self._ollama_sig:
            self._ollama = OllamaChat(sig[1], sig[2], sig[3], self._toolbox, on_tool=self._on_tool)
            self._ollama_sig = sig
        return self._ollama

    def _get_client(self):
        key = get_api_key()
        if not key:
            return None
        if self._client is None or key != self._client_key:
            import anthropic

            self._client = anthropic.Anthropic(api_key=key, max_retries=2, timeout=120)
            self._client_key = key
        return self._client

    def ask(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        provider = self.provider()
        if provider in ("openai", "gigachat"):
            with self._lock:
                try:
                    return self._get_cloud().ask(text)
                except CloudError as exc:
                    if parse_command(text) is not None:
                        return self._local(text)
                    return str(exc)
        if provider == "ollama":
            with self._lock:
                try:
                    return self._get_ollama().ask(text)
                except OllamaError as exc:
                    # Простые команды работают и без модели.
                    if parse_command(text) is not None:
                        return self._local(text)
                    return str(exc)
        client = self._get_client() if provider == "claude" else None
        if client is None:
            return self._local(text)
        with self._lock:
            return self._ask_claude(client, text)

    # ---------- без ИИ ----------
    def _local(self, text: str) -> str:
        parsed = parse_command(text)
        if parsed is None:
            return (
                "Без ИИ я понимаю только простые команды: время, погода, «открой блокнот», "
                "«громче», «напомни через 10 минут…», «найди закупки проектор». "
                "Включите в настройках бесплатный ИИ Ollama или Claude, чтобы я понимал любые вопросы."
            )
        name, args = parsed
        if self._on_tool:
            self._on_tool(name)
        try:
            result = self._toolbox.execute(name, args)
        except ToolError as exc:
            return str(exc)
        if name == "search_procurements":
            found = self._toolbox.last_results()
            return f"Нашёл {len(found)} закупок. Результаты — на вкладке «Закупки»."
        if name == "procurement_analytics" and self._toolbox.last_results():
            a = analyze(self._toolbox.last_results())
            return f"Всего {a['count']} закупок на {format_money(a['total_sum'])}. Подробности — на вкладке «Закупки»."
        return result

    # ---------- Claude ----------
    def _ask_claude(self, client, text: str) -> str:
        import anthropic

        system = self._system_prompt()
        history_len = len(self._messages)
        self._messages.append({"role": "user", "content": text})
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                response = client.beta.messages.create(
                    model=self._config.get("model", "claude-opus-5-5"),
                    max_tokens=16000,
                    system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                    tools=TOOL_DEFINITIONS,
                    messages=self._messages,
                    output_config={"effort": self._config.get("effort", "low")},
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                )
                if response.stop_reason == "refusal":
                    del self._messages[history_len:]
                    return "Извините, на это я ответить не могу."

                # Ответ добавляется целиком (с блоками thinking и tool_use), как требует API.
                self._messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason == "pause_turn":
                    continue
                if response.stop_reason == "max_tokens":
                    # Оборванный ответ может содержать незавершённый вызов инструмента — убираем этот ход.
                    del self._messages[history_len:]
                    return self._final_text(response.content) or "Ответ получился слишком длинным."

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if response.stop_reason != "tool_use" or not tool_uses:
                    return self._final_text(response.content) or "Готово."

                results = []
                for block in tool_uses:
                    if self._on_tool:
                        self._on_tool(block.name)
                    args = block.input if isinstance(block.input, dict) else {}
                    try:
                        output = self._toolbox.execute(block.name, args)
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": output[:20000]})
                    except ToolError as exc:
                        results.append(
                            {"type": "tool_result", "tool_use_id": block.id, "content": f"Ошибка: {exc}", "is_error": True}
                        )
                    except Exception as exc:  # инструмент не должен ронять разговор
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"Внутренняя ошибка: {exc.__class__.__name__}",
                                "is_error": True,
                            }
                        )
                self._messages.append({"role": "user", "content": results})
            # Последнее сообщение — результаты инструментов без ответа; убираем ход, чтобы история оставалась корректной.
            del self._messages[history_len:]
            return "Задача оказалась слишком длинной, я остановился. Уточните, пожалуйста, запрос."
        except anthropic.AuthenticationError:
            del self._messages[history_len:]
            return "API-ключ Claude не подошёл. Проверьте его в настройках."
        except anthropic.PermissionDeniedError:
            del self._messages[history_len:]
            return "У API-ключа нет доступа к этой модели."
        except anthropic.RateLimitError:
            del self._messages[history_len:]
            return "Слишком много запросов к Claude, попробуйте через минуту."
        except anthropic.BadRequestError as exc:
            del self._messages[history_len:]
            return f"Claude отклонил запрос: {exc.message}"
        except anthropic.APIStatusError as exc:
            del self._messages[history_len:]
            return f"Ошибка сервера Claude ({exc.status_code}). Попробуйте позже."
        except anthropic.APIConnectionError:
            del self._messages[history_len:]
            return "Нет связи с Claude. Проверьте интернет."

    @staticmethod
    def _final_text(content) -> str:
        return "\n".join(b.text for b in content if getattr(b, "type", "") == "text").strip()
