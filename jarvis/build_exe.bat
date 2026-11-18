@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Сначала один раз запустите run.bat
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --windowed --name Jarvis --add-data "ui;ui" --collect-submodules jarvis_app main.py
if errorlevel 1 (
  echo Сборка не удалась.
  pause
  exit /b 1
)
echo.
echo Готово: dist\Jarvis\Jarvis.exe
pause
