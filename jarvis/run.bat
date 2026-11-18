@echo off
setlocal
cd /d "%~dp0"

set "VPY=%~dp0.venv\Scripts\python.exe"
set "VPYW=%~dp0.venv\Scripts\pythonw.exe"

if exist "%VPY%" goto run

echo First run: creating Python 3.12 environment and installing libraries (2-3 minutes)...
py -3.12 -m venv "%~dp0.venv"
if errorlevel 1 goto novenv
if not exist "%VPY%" goto novenv
"%VPY%" -m pip install --upgrade pip
"%VPY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto noinstall

:run
start "" "%VPYW%" "%~dp0main.py"
exit /b 0

:novenv
echo ERROR: could not create the environment. Is Python 3.12 installed? Check with: py -0
pause
exit /b 1

:noinstall
echo ERROR: library installation failed. Send the error text above.
rmdir /s /q "%~dp0.venv"
pause
exit /b 1
