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
