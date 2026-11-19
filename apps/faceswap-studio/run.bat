@echo off
rem FaceSwap Studio - запуск. Просто дважды щёлкните этот файл.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Не найдено окружение .venv - сначала запустите install.bat
    echo.
    pause
    exit /b 1
)
echo Запускаю FaceSwap Studio: http://127.0.0.1:8000
echo Браузер откроется сам, когда сервер будет готов. Чтобы остановить сервер, закройте это окно.
echo.
rem Ждём, пока сервер ответит (до 2 минут), и открываем браузер
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "for ($i = 0; $i -lt 120; $i++) { try { [void](Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri 'http://127.0.0.1:8000/api/health'); break } catch { Start-Sleep -Seconds 1 } }; Start-Process 'http://127.0.0.1:8000'"
".venv\Scripts\python.exe" -m app.main %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo Сервер остановился с ошибкой ^(код %RC%^). Прочитайте сообщение выше.
    pause
)
exit /b %RC%
