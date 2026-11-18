@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run run.bat once first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m jarvis_app.shortcut
if errorlevel 1 (
  echo Could not create the shortcut.
  pause
  exit /b 1
)
echo Shortcut J.A.R.V.I.S. created on the Desktop.
pause
