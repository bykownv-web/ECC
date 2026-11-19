#!/usr/bin/env bash
# FaceSwap Studio - запуск на Linux и macOS.
#   ./run.sh            - обычный запуск
#   ./run.sh --mock     - без моделей (FSS_BACKEND=mock)
# Остальные параметры передаются в app.main, например: ./run.sh --port 8080
set -u

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR" || exit 1

ARGS=()
PORT=8000
while [ $# -gt 0 ]; do
    case "$1" in
        --mock) export FSS_BACKEND=mock ;;
        --port)
            ARGS+=("$1" "${2:-8000}")
            PORT="${2:-8000}"
            shift
            ;;
        *) ARGS+=("$1") ;;
    esac
    shift
done

if [ ! -x ".venv/bin/python" ]; then
    echo "Не найдено окружение .venv - сначала запустите ./install.sh"
    exit 1
fi

URL="http://127.0.0.1:$PORT"
echo "Запускаю FaceSwap Studio: $URL (остановить - Ctrl+C)"

open_browser() {
    local i
    for i in $(seq 1 120); do
        if command -v curl >/dev/null 2>&1; then
            curl -fsS -o /dev/null --max-time 2 "$URL/api/health" 2>/dev/null && break
        elif [ "$i" -ge 5 ]; then
            break
        fi
        sleep 1
    done
    if command -v open >/dev/null 2>&1 && [ "$(uname -s)" = "Darwin" ]; then
        open "$URL"
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$URL" >/dev/null 2>&1
    fi
}
open_browser &

exec .venv/bin/python -m app.main ${ARGS[@]+"${ARGS[@]}"}
