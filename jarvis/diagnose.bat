@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run run.bat once first.
  pause
  exit /b 1
)
set "NUM=%~1"
if "%NUM%"=="" set /p NUM=Purchase number or link: 
set "AI="
set /p AI=Also run AI analysis? It can take several minutes [y/N]: 
if /i "%AI%"=="y" (
  ".venv\Scripts\python.exe" diagnose.py "%NUM%" --ai
) else (
  ".venv\Scripts\python.exe" diagnose.py "%NUM%"
)
pause
