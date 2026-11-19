@echo off
rem Image Studio - запуск без моделей (только интерфейс, FSS_BACKEND=mock).
setlocal
set "FSS_BACKEND=mock"
call "%~dp0run.bat" %*
