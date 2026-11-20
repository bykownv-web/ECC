import requests

from jarvis_app import brain as brain_mod
from jarvis_app.config import Config
from jarvis_app.ollama_brain import OllamaChat, OllamaError, ollama_tools
from jarvis_app.tools import ToolBox


class FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, replies):
        self.replies = list(replies)
        self.bodies = []

    def post(self, url, json=None, timeout=None):
        self.bodies.append({"url": url, **json, "messages": list(json["messages"])})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def make(tmp_path, replies):
    config = Config(tmp_path / "c.json")
    toolbox = ToolBox(config, lambda e, p: None)
    session = FakeSession(replies)
    chat = OllamaChat("http://127.0.0.1:11434", "qwen2.5:7b", "SYS", toolbox, session=session)
    return chat, session


def test_tool_call_roundtrip(tmp_path):
    chat, session = make(tmp_path, [
        FakeResp(200, {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "get_time", "arguments": {}}}]}}),
        FakeResp(200, {"message": {"role": "assistant", "content": "Сейчас полдень, сэр."}}),
    ])
    assert chat.ask("который час") == "Сейчас полдень, сэр."
    first, second = session.bodies
    assert first["url"].endswith("/api/chat") and first["model"] == "qwen2.5:7b"
    assert first["messages"][0] == {"role": "system", "content": "SYS"}
    assert first["stream"] is False and first["tools"][0]["type"] == "function"
    tool_msg = second["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_name"] == "get_time"
    assert tool_msg["content"].startswith("Сейчас ")


def test_string_arguments_are_parsed(tmp_path):
    chat, session = make(tmp_path, [
        FakeResp(200, {"message": {"content": "", "tool_calls": [
            {"function": {"name": "open_url", "arguments": '{"url": "ftp://x"}'}}]}}),
        FakeResp(200, {"message": {"content": "Не вышло."}}),
    ])
    assert chat.ask("открой") == "Не вышло."
    assert session.bodies[1]["messages"][-1]["content"].startswith("Ошибка:")


def test_not_running_and_missing_model(tmp_path):
    chat, _ = make(tmp_path, [requests.ConnectionError()])
    try:
        chat.ask("привет")
        raise AssertionError("ожидалась ошибка")
    except OllamaError as exc:
        assert "не запущена" in str(exc)
    assert chat._messages == []
    chat, _ = make(tmp_path, [FakeResp(404, text="model not found")])
    try:
        chat.ask("привет")
    except OllamaError as exc:
        assert "ollama pull qwen2.5:7b" in str(exc)


def test_brain_falls_back_to_local_commands_when_ollama_down(tmp_path, monkeypatch):
    config = Config(tmp_path / "c.json")
    toolbox = ToolBox(config, lambda e, p: None)
    b = brain_mod.Brain(config, toolbox)
    assert b.provider() == "ollama"

    def down(self, text):
        raise OllamaError("Ollama не запущена.")

    monkeypatch.setattr(OllamaChat, "ask", down)
    assert b.ask("который час").startswith("Сейчас ")
    assert b.ask("расскажи анекдот") == "Ollama не запущена."


def test_tools_schema_shape():
    tools = ollama_tools()
    assert all(t["function"]["parameters"]["type"] == "object" for t in tools)
