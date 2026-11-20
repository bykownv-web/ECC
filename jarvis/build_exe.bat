@echo off
setlocal
cd /d "%~dp0"
set "VPY=%~dp0.venv\Scripts\python.exe"
if exist "%VPY%" goto build
echo Run run.bat once first.
pause
exit /b 1

:build
"%VPY%" -m pip install -r "%~dp0requirements-dev.txt"
"%VPY%" -m PyInstaller --noconfirm --windowed --name Jarvis --add-data "ui;ui" --add-data "assets;assets" --icon "%~dp0assets\jarvis.ico" --collect-submodules jarvis_app "%~dp0main.py"
if errorlevel 1 goto fail
echo.
echo Done: dist\Jarvis\Jarvis.exe
pause
exit /b 0

:fail
echo Build failed.
pause
exit /b 1
