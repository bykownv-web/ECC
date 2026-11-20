import json

from jarvis_app.cloud_llm import GigaChat, OpenAICompatChat


class Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p


class Sess:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def post(self, url, json=None, data=None, headers=None, timeout=None, verify=None):
        self.calls.append({"url": url, "json": json, "data": data, "headers": headers})
        return self.replies.pop(0)


class TB:
    def __init__(self):
        self.ran = []

    def execute(self, name, args):
        self.ran.append((name, args))
        return "Сейчас 12:00."


def test_openai_compatible_tool_loop():
    sess = Sess([
        Resp({"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "get_time", "arguments": "{}"}}]}}]}),
        Resp({"choices": [{"message": {"role": "assistant", "content": "Полдень, сэр."}}]}),
    ])
    tb = TB()
    chat = OpenAICompatChat("https://openrouter.ai/api/v1/", "nousresearch/hermes-4-70b", "SYS", tb, session=sess, api_key="k")
    assert chat.ask("который час") == "Полдень, сэр."
    first, second = sess.calls
    assert first["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert first["headers"]["Authorization"] == "Bearer k"
    assert first["json"]["model"] == "nousresearch/hermes-4-70b" and first["json"]["tools"][0]["type"] == "function"
    assert second["json"]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "Сейчас 12:00."}
    assert tb.ran == [("get_time", {})]


def test_openai_json_falls_back_without_response_format():
    sess = Sess([Resp({"error": {"message": "response_format not supported"}}, status=400),
                 Resp({"choices": [{"message": {"content": '```json\n{"ok": true}\n```'}}]})])
    chat = OpenAICompatChat("https://api.example/v1", "m", "", session=sess, api_key="k")
    assert chat.json("S", "P", {"type": "object"}) == {"ok": True}
    assert "response_format" in sess.calls[0]["json"] and "response_format" not in sess.calls[1]["json"]


def test_gigachat_oauth_and_function_call():
    sess = Sess([
        Resp({"access_token": "TOKEN", "expires_at": 9999999999999}),
        Resp({"choices": [{"message": {"role": "assistant", "content": "", "function_call": {"name": "get_time", "arguments": {}},
                                        "functions_state_id": "st1"}, "finish_reason": "function_call"}]}),
        Resp({"choices": [{"message": {"role": "assistant", "content": "Двенадцать часов."}}]}),
    ])
    tb = TB()
    gc = GigaChat("GigaChat-2", "SYS", tb, session=sess, auth_key="QUJD")
    assert gc.ask("время") == "Двенадцать часов."
    oauth, first, second = sess.calls
    assert oauth["headers"]["Authorization"] == "Basic QUJD" and oauth["data"] == {"scope": "GIGACHAT_API_PERS"}
    assert first["headers"]["Authorization"] == "Bearer TOKEN" and first["json"]["functions"][0]["name"]
    fn_msg = second["json"]["messages"][-1]
    assert fn_msg["role"] == "function" and fn_msg["name"] == "get_time"
    assert json.loads(fn_msg["content"]) == {"result": "Сейчас 12:00."}
    assert second["json"]["messages"][-2]["functions_state_id"] == "st1"
    # токен переиспользуется, повторной авторизации нет
    sess.replies.append(Resp({"choices": [{"message": {"content": "{\"a\": 1}"}}]}))
    assert gc.json("S", "P", {"type": "object"}) == {"a": 1}
    assert len(sess.calls) == 4


def test_errors_are_human_readable():
    sess = Sess([Resp({"error": {"message": "bad key"}}, status=401)])
    chat = OpenAICompatChat("https://api.example/v1", "m", "", TB(), session=sess, api_key="k")
    try:
        chat.ask("привет")
    except Exception as exc:
        assert "ключ не подошёл" in str(exc)
    assert chat._messages == []


def test_gigachat_413_resets_history_and_retries():
    sess = Sess([
        Resp({"access_token": "T", "expires_at": 9999999999999}),
        Resp({"message": "Payload Too Large"}, status=413),
        Resp({"choices": [{"message": {"content": "Готово."}}]}),
    ])
    gc = GigaChat("GigaChat-2", "SYS", TB(), session=sess, auth_key="K")
    gc._messages = [{"role": "user", "content": "старое"}, {"role": "assistant", "content": "x" * 50000}]
    assert gc.ask("новый вопрос") == "Готово."
    retry = sess.calls[-1]["json"]["messages"]
    assert [m["role"] for m in retry] == ["system", "user"] and retry[-1]["content"] == "новый вопрос"


def test_gigachat_truncated_answer_is_retried_and_normalized():
    sess = Sess([
        Resp({"access_token": "T", "expires_at": 9999999999999}),
        Resp({"choices": [{"message": {"content": '{"scores": [{"number": "1", "score": "8'}, "finish_reason": "length"}]}),
        Resp({"choices": [{"message": {"content": '```json\n{"scores": [{"number": 1, "score": "85%",}]}\n```'}}]}),
    ])
    gc = GigaChat("GigaChat-2", "", session=sess, auth_key="K")
    schema = {"type": "object", "properties": {"scores": {"type": "array", "items": {"type": "object", "properties": {
        "number": {"type": "string"}, "score": {"type": "integer"}, "reason": {"type": "string"}}}}}}
    assert gc.json("S", "P", schema) == {"scores": [{"number": "1", "score": 85, "reason": ""}]}
    assert "оборвался" in sess.calls[-1]["json"]["messages"][-1]["content"]


def test_unexpected_transport_error_rolls_back_history():
    import requests

    class Boom:
        def post(self, *a, **k):
            raise requests.exceptions.ChunkedEncodingError("cut")

    chat = OpenAICompatChat("https://api.example/v1", "m", "", TB(), session=Boom(), api_key="k")
    try:
        chat.ask("привет")
    except Exception as exc:
        assert "Ошибка связи" in str(exc)
    assert chat._messages == []
