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
