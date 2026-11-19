from types import SimpleNamespace

import pytest

from jarvis_app import brain as brain_mod
from jarvis_app.config import Config
from jarvis_app.tools import TOOL_DEFINITIONS, ToolBox


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(brain_mod, "get_api_key", lambda: "sk-ant-test")
    events = []
    config = Config(tmp_path / "config.json")
    config.update({"ai_provider": "claude"})
    toolbox = ToolBox(config, lambda e, p: events.append((e, p)))
    return config, toolbox, events


def block(**kw):
    return SimpleNamespace(**kw)


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def make_brain(config, toolbox, responses):
    b = brain_mod.Brain(config, toolbox)
    fake = FakeMessages(responses)
    b._client = SimpleNamespace(beta=SimpleNamespace(messages=fake))
    b._client_key = "sk-ant-test"
    return b, fake


def test_tool_loop_runs_tool_and_returns_text(env):
    config, toolbox, _ = env
    responses = [
        SimpleNamespace(stop_reason="tool_use", content=[
            block(type="text", text="Смотрю."),
            block(type="tool_use", id="tu_1", name="get_time", input={}),
        ]),
        SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="Сейчас полдень, сэр.")]),
    ]
    b, fake = make_brain(config, toolbox, responses)
    assert b.ask("который час") == "Сейчас полдень, сэр."
    first, second = fake.calls
    assert first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default"
    assert first["betas"] == [brain_mod.FALLBACK_BETA]
    assert first["output_config"] == {"effort": "low"}
    tool_result = second["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "tu_1"
    assert tool_result["content"].startswith("Сейчас ")
    # история: user, assistant(tool_use), user(tool_result), assistant(final)
    assert [m["role"] for m in b._messages] == ["user", "assistant", "user", "assistant"]


def test_tool_error_is_reported_to_model(env):
    config, toolbox, _ = env
    responses = [
        SimpleNamespace(stop_reason="tool_use", content=[
            block(type="tool_use", id="tu_1", name="open_url", input={"url": "javascript:alert(1)"}),
        ]),
        SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="Не получилось.")]),
    ]
    b, fake = make_brain(config, toolbox, responses)
    assert b.ask("открой сайт") == "Не получилось."
    result = fake.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True


def test_refusal_rolls_back_history(env):
    config, toolbox, _ = env
    b, _ = make_brain(config, toolbox, [SimpleNamespace(stop_reason="refusal", content=[])])
    assert "не могу" in b.ask("что-то")
    assert b._messages == []


def test_local_mode_without_key(env, monkeypatch):
    config, toolbox, _ = env
    monkeypatch.setattr(brain_mod, "get_api_key", lambda: "")
    b = brain_mod.Brain(config, toolbox)
    assert b.ask("который час").startswith("Сейчас ")
    assert "простые команды" in b.ask("расскажи анекдот")


def test_tool_schemas_are_well_formed():
    names = set()
    for tool in TOOL_DEFINITIONS:
        assert tool["name"] not in names
        names.add(tool["name"])
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert set(schema["required"]) <= set(schema["properties"])


# ---------- регрессии: история Claude, переключение провайдеров, запасной режим ----------

def test_unexpected_tool_loop_error_rolls_back_history(env):
    config, toolbox, _ = env

    class Exploding(FakeMessages):
        def create(self, **kwargs):
            if len(self.calls) == 1:
                self.calls.append(kwargs)
                raise RuntimeError("SDK сломался")
            return super().create(**kwargs)

    b = brain_mod.Brain(config, toolbox)
    fake = Exploding([SimpleNamespace(stop_reason="tool_use", content=[
        block(type="tool_use", id="tu_1", name="get_time", input={}),
    ])])
    b._client = SimpleNamespace(beta=SimpleNamespace(messages=fake))
    b._client_key = "sk-ant-test"
    answer = b.ask("расскажи анекдот")
    assert "RuntimeError" in answer
    assert b._messages == []  # не осталось tool_use без tool_result


def test_tool_use_without_tool_use_stop_reason_still_gets_result(env):
    config, toolbox, _ = env
    responses = [
        SimpleNamespace(stop_reason="end_turn", content=[block(type="tool_use", id="tu_9", name="get_time", input={})]),
        SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="Готово, сэр.")]),
    ]
    b, fake = make_brain(config, toolbox, responses)
    assert b.ask("время") == "Готово, сэр."
    assert fake.calls[1]["messages"][-1]["content"][0]["tool_use_id"] == "tu_9"


def test_non_string_tool_output_is_converted(env, monkeypatch):
    config, toolbox, _ = env
    monkeypatch.setattr(toolbox, "execute", lambda name, args: None)
    responses = [
        SimpleNamespace(stop_reason="tool_use", content=[block(type="tool_use", id="tu_1", name="get_time", input={})]),
        SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="ок")]),
    ]
    b, fake = make_brain(config, toolbox, responses)
    assert b.ask("время") == "ок"
    result = fake.calls[1]["messages"][-1]["content"][0]
    assert result["content"] == "Готово." and "is_error" not in result


def test_claude_connection_error_falls_back_to_local_command(env):
    import anthropic

    config, toolbox, _ = env

    class Offline(FakeMessages):
        def create(self, **kwargs):
            exc = anthropic.APIConnectionError.__new__(anthropic.APIConnectionError)
            Exception.__init__(exc, "offline")
            raise exc

    b = brain_mod.Brain(config, toolbox)
    b._client = SimpleNamespace(beta=SimpleNamespace(messages=Offline([])))
    b._client_key = "sk-ant-test"
    assert b.ask("который час").startswith("Сейчас ")
    assert "Нет связи" in b.ask("расскажи анекдот")
    assert b._messages == []


class FakeChat:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.answer: object = "ответ"
        self.reset_called = False

    def ask(self, text):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def reset(self):
        self.reset_called = True


def test_provider_switching_and_cached_chat(env, monkeypatch):
    config, toolbox, _ = env
    monkeypatch.setattr(brain_mod, "OllamaChat", FakeChat)
    monkeypatch.setattr(brain_mod, "GigaChat", FakeChat)
    monkeypatch.setattr(brain_mod, "OpenAICompatChat", FakeChat)
    b = brain_mod.Brain(config, toolbox)
    seen = []
    for provider in ("ollama", "ollama", "gigachat", "openai", "openai"):
        config.update({"ai_provider": provider})
        assert b.ask("привет") == "ответ"
        seen.append((provider, b._ollama))
    assert seen[0][1] is seen[1][1]  # тот же чат, пока настройки не менялись
    assert len({id(c) for _, c in seen}) == 3
    config.update({"openai_model": "другая"})
    b.ask("привет")
    assert b._ollama is not seen[-1][1]
    config.update({"ai_provider": "none"})
    assert "простые команды" in b.ask("расскажи анекдот")
    config.update({"ai_provider": "что-то странное"})
    assert b.provider() == "ollama"


def test_reset_recreates_chat_so_new_keys_apply(env, monkeypatch):
    config, toolbox, _ = env
    monkeypatch.setattr(brain_mod, "GigaChat", FakeChat)
    config.update({"ai_provider": "gigachat"})
    b = brain_mod.Brain(config, toolbox)
    b.ask("привет")
    old = b._ollama
    b.reset()
    b.ask("привет")
    assert b._ollama is not old  # GigaChat кэширует токен — новый ключ подхватится только новым объектом


def test_chat_errors_fall_back_to_local(env, monkeypatch):
    config, toolbox, _ = env
    monkeypatch.setattr(brain_mod, "OllamaChat", FakeChat)
    config.update({"ai_provider": "ollama"})
    b = brain_mod.Brain(config, toolbox)
    b.ask("привет")
    b._ollama.answer = brain_mod.OllamaError("Ollama не запущена.")
    assert b.ask("который час").startswith("Сейчас ")
    assert b.ask("расскажи анекдот") == "Ollama не запущена."
    b._ollama.answer = TypeError("неожиданно")
    assert b.ask("который час").startswith("Сейчас ")
    assert b._ollama is None  # чат с возможно битой историей сброшен
    b.ask("привет")
    b._ollama.answer = TypeError("неожиданно")
    assert "TypeError" in b.ask("расскажи анекдот")


def test_gigachat_without_key_is_not_reported_as_ai(env, monkeypatch):
    config, toolbox, _ = env
    monkeypatch.setattr(brain_mod, "get_secret", lambda name: "")
    config.update({"ai_provider": "gigachat"})
    assert brain_mod.Brain(config, toolbox).has_ai() is False
    monkeypatch.setattr(brain_mod, "get_secret", lambda name: "key")
    assert brain_mod.Brain(config, toolbox).has_ai() is True
