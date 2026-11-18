"""Голос: распознавание речи (SpeechRecognition + Google, русский) и озвучка (Windows SAPI).

Озвучка идёт через встроенный в Windows System.Speech в отдельном процессе PowerShell:
никаких дополнительных библиотек, и речь можно прервать в любой момент.
Текст передаётся через переменную окружения, а не через командную строку.
"""

from __future__ import annotations

import os
import platform
import subprocess
import threading
from typing import Callable

IS_WINDOWS = platform.system() == "Windows"
CREATE_NO_WINDOW = 0x08000000

_TTS_SCRIPT = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try { $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Male, [System.Speech.Synthesis.VoiceAge]::Adult, 0, [System.Globalization.CultureInfo]'ru-RU') } catch {}
$ru = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'ru-RU' } | Select-Object -First 1
if ($ru -and $s.Voice.Culture.Name -ne 'ru-RU') { $s.SelectVoice($ru.VoiceInfo.Name) }
$s.Rate = 1
$s.Speak($env:JARVIS_TTS_TEXT)
"""


class Speaker:
    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self.enabled = True

    def say(self, text: str) -> None:
        if not (self.enabled and IS_WINDOWS and text.strip()):
            return
        self.stop()
        env = dict(os.environ, JARVIS_TTS_TEXT=text[:3000])
        with self._lock:
            try:
                self._proc = subprocess.Popen(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", _TTS_SCRIPT],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
            except OSError:
                self._proc = None

    def stop(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.kill()
            self._proc = None

    def is_speaking(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None


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
