@echo off
chcp 65001 >nul
cd /d "%~dp0"

where py >/dev/null 2>nul
if errorlevel 1 (
  echo Python не найден. Установите Python 3.12 с python.org и отметьте "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Первый запуск: создаю окружение и ставлю библиотеки, это займёт пару минут...
  py -3.12 -m venv .venv 2>/dev/null || py -3 -m venv .venv
  if errorlevel 1 (
    echo Не удалось создать окружение Python.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Ошибка установки библиотек. Пришлите текст ошибки выше.
    rmdir /s /q .venv
    pause
    exit /b 1
  )
)

start "" ".venv\Scripts\pythonw.exe" main.py
