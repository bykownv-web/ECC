from jarvis_app import voice


def test_configure_validates_values():
    s = voice.Speaker()
    s.configure("ru-RU-DmitryNeural", "+10%", "-15Hz")
    assert (s.voice, s.rate, s.pitch) == ("ru-RU-DmitryNeural", "+10%", "-15Hz")
    s.configure("evil; rm", "fast", "$(x)")
    assert (s.voice, s.rate, s.pitch) == ("ru-RU-DmitryNeural", "+0%", "-8Hz")


def test_neural_failure_falls_back_to_system_voice(monkeypatch):
    s = voice.Speaker()
    calls = []

    def broken(text, stop):
        raise OSError("нет интернета")

    monkeypatch.setattr(s, "_speak_neural", broken)
    monkeypatch.setattr(s, "_speak_sapi", lambda text, stop: calls.append(text))
    s._speak("Привет", voice.threading.Event())
    assert calls == ["Привет"]


def test_sapi_choice_skips_neural(monkeypatch):
    s = voice.Speaker()
    s.configure("sapi", "+0%", "+0Hz")
    monkeypatch.setattr(s, "_speak_neural", lambda *a: (_ for _ in ()).throw(AssertionError("не должен вызываться")))
    called = []
    monkeypatch.setattr(s, "_speak_sapi", lambda text, stop: called.append(text))
    s._speak("Тест", voice.threading.Event())
    assert called == ["Тест"]


class FakeResponse:
    def __init__(self, status, content=b"", text=""):
        self.status_code = status
        self.content = content
        self.text = text


def test_elevenlabs_success_plays_audio(monkeypatch):
    import requests

    s = voice.Speaker()
    s.configure("elevenlabs", "+0%", "+0Hz", "JBFqnCBsd6RMkjVDRZzb")
    s.eleven_key = lambda: "test-key-0123456789abcdef"
    sent = {}

    def fake_post(url, **kw):
        sent.update(url=url, **kw)
        return FakeResponse(200, b"ID3fake-mp3")

    played = []
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(voice, "_play_mp3", lambda path, stop: played.append(path.read_bytes()))
    s._speak("Добрый день", voice.threading.Event())
    assert played == [b"ID3fake-mp3"]
    assert sent["url"].endswith("/JBFqnCBsd6RMkjVDRZzb")
    assert sent["headers"]["xi-api-key"] == "test-key-0123456789abcdef"
    assert sent["json"]["model_id"] == "eleven_multilingual_v2"


def test_elevenlabs_bad_key_reports_once_and_falls_back(monkeypatch):
    import requests

    s = voice.Speaker()
    s.configure("elevenlabs", "+0%", "+0Hz")
    s.eleven_key = lambda: "test-key-0123456789abcdef"
    errors, fallback = [], []
    s.on_error = errors.append
    monkeypatch.setattr(requests, "post", lambda url, **kw: FakeResponse(401))
    monkeypatch.setattr(s, "_speak_neural", lambda text, stop: fallback.append(text))
    s._speak("раз", voice.threading.Event())
    s._speak("два", voice.threading.Event())
    assert fallback == ["раз", "два"]
    assert len(errors) == 1 and "ключ" in errors[0]


def test_elevenlabs_invalid_voice_id_uses_default():
    s = voice.Speaker()
    s.configure("elevenlabs", "+0%", "+0Hz", "../../etc")
    assert s.eleven_voice_id == voice.ELEVEN_DEFAULT_VOICE
