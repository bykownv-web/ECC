"""Голос: распознавание речи (SpeechRecognition + Google, русский) и озвучка.

Озвучка по умолчанию — нейросетевой голос Microsoft (edge-tts, нужен интернет), мужской
«Дмитрий» с чуть пониженным тоном. Звук проигрывается встроенным в Windows MCI, без
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

VOICES = {
    "ru-RU-DmitryNeural": "Дмитрий — мужской, нейросетевой",
    "ru-RU-SvetlanaNeural": "Светлана — женский, нейросетевой",
    "sapi": "Системный голос Windows (без интернета)",
}

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


class Speaker:
    def __init__(self) -> None:
        self.enabled = True
        self.voice = "ru-RU-DmitryNeural"
        self.rate = "+0%"
        self.pitch = "-8Hz"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def configure(self, voice: str, rate: str, pitch: str) -> None:
        self.voice = voice if voice in VOICES else "ru-RU-DmitryNeural"
        self.rate = rate if re.fullmatch(r"[+-]\d{1,3}%", rate or "") else "+0%"
        self.pitch = pitch if re.fullmatch(r"[+-]\d{1,3}Hz", pitch or "") else "-8Hz"

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

    def _speak(self, text: str, stop: threading.Event) -> None:
        if self.voice != "sapi":
            try:
                self._speak_neural(text, stop)
                return
            except Exception:
                if stop.is_set():
                    return  # прервали — не переключаемся на запасной голос
        self._speak_sapi(text, stop)

    def _speak_neural(self, text: str, stop: threading.Event) -> None:
        import edge_tts

        path = Path(tempfile.gettempdir()) / f"jarvis_tts_{uuid.uuid4().hex}.mp3"
        try:
            asyncio.run(edge_tts.Communicate(text, self.voice, rate=self.rate, pitch=self.pitch).save(str(path)))
            if stop.is_set():
                return
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
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _speak_sapi(self, text: str, stop: threading.Event) -> None:
        env = dict(os.environ, JARVIS_TTS_TEXT=text)
        try:
            proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", _TTS_SCRIPT],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError:
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
            with self._microphone() as source:
                self._recognizer.adjust_for_ambient_noise(source, duration=0.4)
                try:
                    audio = self._recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_limit)
                except self._sr.WaitTimeoutError:
                    return ""
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
                    stop.wait(5)
                    continue
                if stop.is_set() or not text:
                    continue
                command = extract_after_wake_word(text, wake)
                if command is not None:
                    on_command(command)

        threading.Thread(target=loop, name="jarvis-listener", daemon=True).start()

    def stop_background(self) -> None:
        if self._background_stop is not None:
            self._background_stop.set()
            self._background_stop = None


def extract_after_wake_word(text: str, wake_word: str) -> str | None:
    """'Джарвис, который час' -> 'который час'; без слова-активатора -> None."""
    lowered = text.lower()
    wake = wake_word.lower().strip()
    if not wake:
        return text
    stem = wake[:-1] if len(wake) > 4 else wake  # «джарвис», «джарвиса», «джарвису»
    idx = lowered.find(stem)
    if idx == -1:
        return None
    rest = text[idx + len(stem):]
    while rest and rest[0].isalpha():
        rest = rest[1:]
    return rest.strip(" ,.!?:;—-")
