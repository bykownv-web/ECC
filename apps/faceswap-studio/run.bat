@echo off
rem Image Studio - запуск. Просто дважды щёлкните этот файл.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Не найдено окружение .venv - сначала запустите install.bat
    echo.
    pause
    exit /b 1
)
echo Запускаю Image Studio...
echo Адрес появится ниже, браузер откроется сам, когда сервер будет готов.
echo Если порт 8000 занят, приложение само выберет другой.
echo Чтобы остановить сервер, закройте это окно.
echo.
".venv\Scripts\python.exe" -m app.main --open-browser %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo Сервер остановился с ошибкой ^(код %RC%^). Прочитайте сообщение выше.
    pause
)
exit /b %RC%
