#!/usr/bin/env bash
# Image Studio - запуск на Linux и macOS.
#   ./run.sh            - обычный запуск
#   ./run.sh --mock     - без моделей (FSS_BACKEND=mock)
# Остальные параметры передаются в app.main, например: ./run.sh --port 8080
set -u

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR" || exit 1

ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --mock) export FSS_BACKEND=mock ;;
        *) ARGS+=("$1") ;;
    esac
    shift
done

if [ ! -x ".venv/bin/python" ]; then
    echo "Не найдено окружение .venv - сначала запустите ./install.sh"
    exit 1
fi

echo "Запускаю Image Studio (адрес появится ниже, остановить - Ctrl+C)"

exec .venv/bin/python -m app.main --open-browser ${ARGS[@]+"${ARGS[@]}"}
