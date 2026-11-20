"""Голос: распознавание речи (SpeechRecognition + Google, русский) и озвучка.

Озвучка: ElevenLabs (по ключу пользователя) или нейросетевой голос Microsoft (edge-tts),
мужской «Дмитрий» с чуть пониженным тоном; оба требуют интернет. Звук проигрывается встроенным в Windows MCI, без
дополнительных библиотек. Если нейросетевой голос недоступен, используется системный
голос Windows (System.Speech) в отдельном процессе PowerShell; текст передаётся через
переменную окружения, а не через командную строку.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import platform
import re
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Callable

IS_WINDOWS = platform.system() == "Windows"
CREATE_NO_WINDOW = 0x08000000
LISTEN_RETRY_SEC = 5  # пауза фонового прослушивания после ошибки микрофона

VOICES = {
    "elevenlabs": "ElevenLabs (нужен ключ ElevenLabs)",
    "ru-RU-DmitryNeural": "Дмитрий — мужской, нейросетевой",
    "ru-RU-SvetlanaNeural": "Светлана — женский, нейросетевой",
    "sapi": "Системный голос Windows (без интернета)",
}

# Готовые (premade) голоса ElevenLabs. Это оригинальные голоса сервиса, а не копии чьих-то голосов.
ELEVEN_VOICES = {
    "onwK4e9ZLuTAKqWW03F9": "Daniel — британский, сдержанный",
    "JBFqnCBsd6RMkjVDRZzb": "George — британский, тёплый, низкий",
}
ELEVEN_DEFAULT_VOICE = "onwK4e9ZLuTAKqWW03F9"
ELEVEN_MODEL = "eleven_multilingual_v2"
ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
_VOICE_ID_RE = re.compile(r"[A-Za-z0-9]{10,40}")

_TTS_SCRIPT = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$ru = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'ru-RU' } | Select-Object -First 1
if ($ru) { $s.SelectVoice($ru.VoiceInfo.Name) }
$s.Rate = 1
$s.Speak($env:JARVIS_TTS_TEXT)
"""


def _mci(command: str, check: bool = False) -> str:
    buf = ctypes.create_unicode_buffer(256)
    code = ctypes.windll.winmm.mciSendStringW(command, buf, 255, 0)  # type: ignore[attr-defined]
    if check and code != 0:
        raise RuntimeError(f"MCI error {code} on: {command.split()[0]}")
    return buf.value


def _temp_mp3() -> Path:
    return Path(tempfile.gettempdir()) / f"jarvis_tts_{uuid.uuid4().hex}.mp3"


def _play_mp3(path: Path, stop: threading.Event) -> None:
    alias = f"jarvis{uuid.uuid4().hex[:8]}"
    _mci(f'open "{path}" type mpegvideo alias {alias}', check=True)
    try:
        _mci(f"play {alias}", check=True)
        while not stop.wait(0.1):
            if _mci(f"status {alias} mode") != "playing":
                break
    finally:
        _mci(f"stop {alias}")
        _mci(f"close {alias}")


class TTSError(Exception):
    pass


class Speaker:
    def __init__(self) -> None:
        self.enabled = True
        self.voice = "ru-RU-DmitryNeural"
        self.rate = "+0%"
        self.pitch = "-8Hz"
        self.eleven_voice_id = ELEVEN_DEFAULT_VOICE
        self.eleven_key: Callable[[], str] = lambda: ""
        self.on_error: Callable[[str], None] | None = None
        self._last_error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def configure(self, voice: str, rate: str, pitch: str, eleven_voice_id: str = "") -> None:
        self.voice = voice if voice in VOICES else "ru-RU-DmitryNeural"
        self.rate = rate if re.fullmatch(r"[+-]\d{1,3}%", rate or "") else "+0%"
        self.pitch = pitch if re.fullmatch(r"[+-]\d{1,3}Hz", pitch or "") else "-8Hz"
        vid = (eleven_voice_id or "").strip()
        self.eleven_voice_id = vid if _VOICE_ID_RE.fullmatch(vid) else ELEVEN_DEFAULT_VOICE
        self._last_error = ""

    def say(self, text: str) -> None:
        text = (text or "").strip()
        if not (self.enabled and IS_WINDOWS and text):
            return
        self.stop()
        stop = threading.Event()
        with self._lock:
            self._stop = stop
            self._thread = threading.Thread(target=self._speak, args=(text[:3000], stop), daemon=True)
            self._thread.start()

    def _report(self, message: str) -> None:
        # Об одной и той же проблеме сообщаем один раз, а не после каждого ответа.
        if message != self._last_error:
            self._last_error = message
            if self.on_error:
                self.on_error(message)

    def _speak(self, text: str, stop: threading.Event) -> None:
        """Пробует выбранный голос, затем запасные: ElevenLabs → Дмитрий → системный."""
        chain: list[Callable[[str, threading.Event], None]] = []
        if self.voice == "elevenlabs":
            chain = [self._speak_elevenlabs, self._speak_neural]
        elif self.voice != "sapi":
            chain = [self._speak_neural]
        for engine in chain:
            try:
                engine(text, stop)
                return
            except TTSError as exc:
                self._report(str(exc))
            except ImportError:
                self._report("Нейросетевой голос недоступен: не установлен пакет edge-tts. Говорю системным голосом.")
            except Exception as exc:  # не молчим: иначе непонятно, почему голос сменился на системный
                self._report(f"Голос недоступен ({exc.__class__.__name__}), говорю запасным.")
            if stop.is_set():
                return  # прервали — не переключаемся на запасной голос
        try:
            self._speak_sapi(text, stop)
        except Exception as exc:
            self._report(f"Озвучка недоступна ({exc.__class__.__name__}).")

    def _speak_elevenlabs(self, text: str, stop: threading.Event) -> None:
        import requests

        key = self.eleven_key()
        if not key:
            raise TTSError("Голос ElevenLabs: не задан ключ. Добавьте его в настройках.")
        try:
            response = requests.post(
                ELEVEN_URL.format(voice_id=self.eleven_voice_id),
                params={"output_format": "mp3_44100_128"},
                headers={"xi-api-key": key, "Accept": "audio/mpeg"},
                json={
                    "text": text,
                    "model_id": ELEVEN_MODEL,
                    "voice_settings": {"stability": 0.55, "similarity_boost": 0.75, "style": 0.15},
                },
                timeout=40,
            )
        except requests.RequestException as exc:
            raise TTSError(f"Голос ElevenLabs недоступен ({exc.__class__.__name__}), говорю запасным.") from exc
        if response.status_code == 401:
            raise TTSError("Голос ElevenLabs: ключ не подошёл. Проверьте его в настройках.")
        if response.status_code == 402 or (response.status_code == 403 and "quota" in response.text.lower()):
            raise TTSError("Голос ElevenLabs: закончились кредиты на тарифе, говорю запасным голосом.")
        if response.status_code == 404:
            raise TTSError("Голос ElevenLabs: голос с таким ID не найден.")
        if response.status_code != 200 or not response.content:
            raise TTSError(f"Голос ElevenLabs: ошибка {response.status_code}, говорю запасным голосом.")
        if stop.is_set():
            return
        path = _temp_mp3()
        try:
            path.write_bytes(response.content)
            _play_mp3(path, stop)
        finally:
            path.unlink(missing_ok=True)

    def _speak_neural(self, text: str, stop: threading.Event) -> None:
        import edge_tts

        voice = self.voice if self.voice.endswith("Neural") else "ru-RU-DmitryNeural"
        path = _temp_mp3()
        try:
            asyncio.run(edge_tts.Communicate(text, voice, rate=self.rate, pitch=self.pitch).save(str(path)))
            if not stop.is_set():
                _play_mp3(path, stop)
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _speak_sapi(self, text: str, stop: threading.Event) -> None:
        if not IS_WINDOWS:
            return  # системный голос есть только в Windows (creationflags на других ОС — ошибка)
        env = dict(os.environ, JARVIS_TTS_TEXT=text)
        try:
            proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", _TTS_SCRIPT],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, ValueError):
            return
        with self._lock:
            self._proc = proc
        while proc.poll() is None:
            if stop.wait(0.1):
                proc.kill()
                break

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            thread = self._thread
            proc = self._proc
            self._proc = None
        if proc is not None and proc.poll() is None:
            proc.kill()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)

    def is_speaking(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()


class VoiceError(Exception):
    pass


class Listener:
    """Слушает микрофон. Режим «всегда слушать» реагирует только на слово-активатор."""

    def __init__(self) -> None:
        try:
            import speech_recognition as sr
        except ImportError as exc:
            raise VoiceError("Не установлен пакет SpeechRecognition.") from exc
        self._sr = sr
        self._recognizer = sr.Recognizer()
        self._recognizer.pause_threshold = 0.8
        self._recognizer.dynamic_energy_threshold = True
        self._background_stop: threading.Event | None = None
        self._mic_lock = threading.Lock()

    def _microphone(self):
        try:
            return self._sr.Microphone()
        except (OSError, AttributeError) as exc:
            raise VoiceError("Микрофон не найден или не установлен PyAudio.") from exc

    def _recognize(self, audio) -> str:
        try:
            return self._recognizer.recognize_google(audio, language="ru-RU")
        except self._sr.UnknownValueError:
            return ""
        except self._sr.RequestError as exc:
            raise VoiceError("Сервис распознавания речи недоступен. Проверьте интернет.") from exc

    def listen_once(self, timeout: float = 6, phrase_limit: float = 15) -> str:
        with self._mic_lock:
            microphone = self._microphone()
            try:
                with microphone as source:
                    self._recognizer.adjust_for_ambient_noise(source, duration=0.4)
                    try:
                        audio = self._recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_limit)
                    except self._sr.WaitTimeoutError:
                        return ""
            except (OSError, AttributeError, AssertionError) as exc:
                # PyAudio падает при открытии потока, если микрофон занят или отключён
                raise VoiceError("Не удалось открыть микрофон. Проверьте, что он подключён и не занят.") from exc
        return self._recognize(audio)

    def start_background(
        self,
        wake_word: str,
        on_command: Callable[[str], None],
        on_error: Callable[[str], None],
        is_busy: Callable[[], bool],
    ) -> None:
        self.stop_background()
        stop = threading.Event()
        self._background_stop = stop
        wake = wake_word.lower().strip()

        def loop() -> None:
            while not stop.is_set():
                if is_busy():
                    stop.wait(0.3)
                    continue
                try:
                    text = self.listen_once(timeout=5, phrase_limit=12)
                except VoiceError as exc:
                    on_error(str(exc))
                    stop.wait(LISTEN_RETRY_SEC)
                    continue
                except Exception as exc:  # иначе поток тихо умирает и фоновое прослушивание перестаёт работать
                    on_error(f"Ошибка микрофона ({exc.__class__.__name__}). Пробую снова.")
                    stop.wait(LISTEN_RETRY_SEC)
                    continue
                if stop.is_set() or not text:
                    continue
                command = extract_after_wake_word(text, wake)
                if command is not None:
                    try:
                        on_command(command)
                    except Exception as exc:
                        on_error(f"Не удалось выполнить команду ({exc.__class__.__name__}).")

        threading.Thread(target=loop, name="jarvis-listener", daemon=True).start()

    def stop_background(self) -> None:
        if self._background_stop is not None:
            self._background_stop.set()
            self._background_stop = None


def extract_after_wake_word(text: str, wake_word: str) -> str | None:
    """'Джарвис, который час' -> 'который час'; без слова-активатора -> None."""
    text = text or ""
    lowered = text.lower().replace("ё", "е")
    wake = (wake_word or "").lower().replace("ё", "е").strip()
    if not wake:
        return text
    stem = wake[:-1] if len(wake) > 4 else wake  # «джарвис», «джарвиса», «джарвису»
    idx = lowered.find(stem)
    if idx == -1 and wake == "джарвис":  # распознаватель иногда пишет латиницей
        stem = "jarvis"
        idx = lowered.find(stem)
    if idx == -1:
        return None
    rest = text[idx + len(stem):]
    while rest and rest[0].isalpha():
        rest = rest[1:]
    return rest.strip(" ,.!?:;—-")
