@echo off
rem Image Studio - установка. Просто дважды щёлкните этот файл.
rem Параметры передаются в scripts\install.ps1, например: install.bat -Mock
chcp 65001 >nul
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Установка не завершена ^(код %RC%^). Прочитайте сообщение об ошибке выше.
pause
exit /b %RC%
