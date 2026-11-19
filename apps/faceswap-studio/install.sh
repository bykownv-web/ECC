#!/usr/bin/env bash
# Image Studio - установщик для Linux и macOS.
#
#   ./install.sh                 - полная установка (Linux: PyTorch с CUDA при наличии NVIDIA; macOS: MPS)
#   ./install.sh --mock          - только веб-интерфейс без моделей (FSS_BACKEND=mock)
#   ./install.sh --torch cu121   - принудительно выбрать сборку PyTorch (Linux): cu128, cu124, cu121 или cpu
#
# Скрипт можно запускать повторно: уже сделанные шаги пропускаются.
set -u

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$APP_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
MODELS_DIR="$APP_DIR/models"
TORCH_INDEX_BASE="https://download.pytorch.org/whl/"
PYTHON_DOWNLOAD="https://www.python.org/downloads/release/python-3119/"
TOTAL=6

MOCK=0
TORCH_OVERRIDE="auto"
while [ $# -gt 0 ]; do
    case "$1" in
        --mock | -Mock) MOCK=1 ;;
        --torch | -Torch)
            shift
            TORCH_OVERRIDE="${1:-auto}"
            ;;
        -h | --help)
            sed -n '2,8p' "$0"
            exit 0
            ;;
        *)
            echo "Неизвестный параметр: $1" >&2
            exit 2
            ;;
    esac
    shift
done
case "$TORCH_OVERRIDE" in
    auto | cu128 | cu124 | cu121 | cpu) ;;
    *)
        echo "--torch: допустимо auto, cu128, cu124, cu121 или cpu" >&2
        exit 2
        ;;
esac

OS="$(uname -s)"
SUM_PYTHON="-"
SUM_GPU="не найдена"
SUM_VRAM="-"
SUM_DRIVER="-"
SUM_TORCH_TAG="-"
SUM_TORCH_VER="-"
SUM_ACCEL="нет"
MISSING=()

step() { printf '\n\033[36m[%s/%s] %s\033[0m\n' "$1" "$TOTAL" "$2"; }
ok() { printf '\033[32m      OK: %s\033[0m\n' "$1"; }
info() { printf '      %s\n' "$1"; }
warn() { printf '\033[33m      ВНИМАНИЕ: %s\033[0m\n' "$1"; }
fail() {
    printf '\n\033[31m[ОШИБКА] %s\033[0m\n' "$1"
    [ -n "${2:-}" ] && printf '\033[33mПодсказка: %s\033[0m\n' "$2"
    printf '\033[31mУстановка прервана. Исправьте проблему и снова запустите ./install.sh.\033[0m\n'
    exit 1
}

py_version() {
    "$@" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null
}

# 0 = 3.10-3.12, 1 = слишком новый, 2 = слишком старый
py_supported() {
    local major minor
    major="${1%%.*}"
    minor="${1#*.}"
    minor="${minor%%.*}"
    if [ "$major" = "3" ] && [ "$minor" -ge 10 ] && [ "$minor" -le 12 ]; then
        return 0
    fi
    if [ "$major" = "3" ] && [ "$minor" -ge 13 ]; then
        return 1
    fi
    return 2
}

torch_version() {
    "$VENV_PY" -c 'import torch; print(torch.__version__)' 2>/dev/null | head -n 1
}

# версия драйвера a.b >= c.d ?
version_ge() {
    [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n 1)" = "$2" ]
}

echo "============================================="
echo " Image Studio - установка"
echo "============================================="
echo "Папка приложения: $APP_DIR"
[ "$MOCK" = 1 ] && echo "Режим --mock: только интерфейс, без PyTorch и моделей."

# ----------------------------------------------------------------- 1. Python
step 1 "Поиск Python 3.10-3.12"
PY=""
TOO_NEW=""
TOO_OLD=""
for cand in python3.12 python3.11 python3.10 python3 python; do
    command -v "$cand" >/dev/null 2>&1 || continue
    v="$(py_version "$cand")" || continue
    [ -n "$v" ] || continue
    py_supported "$v"
    case $? in
        0)
            PY="$cand"
            SUM_PYTHON="$v"
            break
            ;;
        1) TOO_NEW="$v" ;;
        *) TOO_OLD="$v" ;;
    esac
done
if [ -z "$PY" ]; then
    if [ -n "$TOO_NEW" ]; then
        fail "Найден только Python $TOO_NEW. Он слишком новый: для 3.13+ пока нет надёжных сборок PyTorch." \
            "установите Python 3.11 ($PYTHON_DOWNLOAD, на Linux - через пакетный менеджер, pyenv или uv)."
    fi
    if [ -n "$TOO_OLD" ]; then
        fail "Найден только Python $TOO_OLD. Нужен Python 3.10, 3.11 или 3.12." "установите Python 3.11: $PYTHON_DOWNLOAD"
    fi
    fail "Python не найден." "установите Python 3.11: $PYTHON_DOWNLOAD"
fi
ok "Python $SUM_PYTHON ($(command -v "$PY"))"

# ----------------------------------------------------------------- 2. venv + pip
step 2 "Виртуальное окружение .venv и обновление pip"
if [ -x "$VENV_PY" ]; then
    vv="$(py_version "$VENV_PY")" || fail "Папка .venv повреждена: $VENV_PY не запускается." "удалите папку $VENV_DIR и снова запустите ./install.sh."
    py_supported "$vv" || fail "В .venv стоит Python $vv, а нужен 3.10-3.12." "удалите папку $VENV_DIR и снова запустите ./install.sh."
    ok ".venv уже есть (Python $vv)"
else
    [ -e "$VENV_DIR" ] && fail "Папка .venv есть, но в ней нет bin/python." "удалите папку $VENV_DIR и снова запустите ./install.sh."
    info "Создаю .venv ..."
    "$PY" -m venv "$VENV_DIR" && [ -x "$VENV_PY" ] ||
        fail "Не удалось создать виртуальное окружение .venv." "на Debian/Ubuntu установите пакет python3-venv (например: sudo apt install python3.11-venv)."
    ok ".venv создано"
fi
info "Обновляю pip ..."
"$VENV_PY" -m pip install --upgrade pip || fail "Не удалось обновить pip." "проверьте подключение к интернету и прокси."
ok "pip обновлён"

# ----------------------------------------------------------------- 3. GPU
step 3 "Поиск ускорителя (NVIDIA / Apple)"
TORCH_TAG="cpu"
if [ "$MOCK" = 1 ]; then
    info "Пропущено (--mock)."
elif [ "$OS" = "Darwin" ]; then
    TORCH_TAG="pypi"
    if [ "$(uname -m)" = "arm64" ]; then
        SUM_GPU="Apple Silicon (MPS)"
        ok "Apple Silicon: PyTorch будет использовать MPS"
    else
        SUM_GPU="Mac на Intel (без MPS)"
        warn "Mac на Intel: ускорения нет, генерация будет очень медленной."
    fi
else
    DRIVER=""
    if ! command -v nvidia-smi >/dev/null 2>&1; then
        warn "nvidia-smi не найден: видеокарты NVIDIA нет или не установлен драйвер."
        warn "Будет установлен PyTorch для CPU - генерация будет ОЧЕНЬ медленной."
    else
        line="$(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>/dev/null | head -n 1)"
        if [ -z "$line" ]; then
            warn "nvidia-smi есть, но не ответил. Возможно, драйвер NVIDIA повреждён. Пока будет PyTorch для CPU."
        else
            IFS=',' read -r g_name g_mem g_drv <<<"$line"
            SUM_GPU="$(echo "$g_name" | xargs)"
            SUM_VRAM="$(echo "$g_mem" | xargs)"
            SUM_DRIVER="$(echo "$g_drv" | xargs)"
            DRIVER="$SUM_DRIVER"
            ok "$SUM_GPU, $SUM_VRAM, драйвер $SUM_DRIVER"
            mib="${SUM_VRAM%% *}"
            case "$mib" in
                '' | *[!0-9]*) ;;
                *) [ "$mib" -lt 7500 ] && warn "Меньше 8 ГБ видеопамяти: берите модели SD 1.5 и размер 512-768." ;;
            esac
        fi
    fi
    if [ -n "$DRIVER" ]; then
        # Минимальные драйверы Linux для CUDA 12.8 / 12.4 / 12.1
        if version_ge "$DRIVER" 570.26; then
            TORCH_TAG="cu128"
        elif version_ge "$DRIVER" 550.54; then
            TORCH_TAG="cu124"
        elif version_ge "$DRIVER" 530.30; then
            TORCH_TAG="cu121"
        else
            warn "Драйвер $DRIVER слишком старый для CUDA 12 (нужен 530+). Будет установлен PyTorch для CPU."
        fi
    fi
    if [ "$TORCH_OVERRIDE" != "auto" ]; then
        info "Сборка PyTorch выбрана вручную: $TORCH_OVERRIDE"
        TORCH_TAG="$TORCH_OVERRIDE"
    fi
    info "Сборка PyTorch: $TORCH_TAG ($TORCH_INDEX_BASE$TORCH_TAG)"
fi

# ----------------------------------------------------------------- 4. torch
step 4 "Установка PyTorch"
if [ "$MOCK" = 1 ]; then
    info "Пропущено (--mock)."
else
    SUM_TORCH_TAG="$TORCH_TAG"
    installed="$(torch_version)"
    installed_tag="pypi"
    case "$installed" in *+*) installed_tag="${installed#*+}" ;; esac
    if [ -n "$installed" ] && [ "$installed_tag" = "$TORCH_TAG" ]; then
        ok "PyTorch $installed уже установлен"
    else
        if [ -n "$installed" ]; then
            info "Сейчас стоит PyTorch $installed, нужна сборка $TORCH_TAG. Переустанавливаю ..."
            "$VENV_PY" -m pip uninstall -y torch torchvision torchaudio
        fi
        info "Скачивание может занять 5-20 минут ..."
        if [ "$TORCH_TAG" = "pypi" ]; then
            "$VENV_PY" -m pip install torch --timeout 120 ||
                fail "Не удалось установить PyTorch." "проверьте интернет и свободное место на диске."
        else
            # Только --index-url: с дополнительным PyPI pip может взять не ту сборку torch.
            "$VENV_PY" -m pip install torch --index-url "$TORCH_INDEX_BASE$TORCH_TAG" --timeout 120 ||
                fail "Не удалось установить PyTorch ($TORCH_TAG)." "проверьте интернет (нужен доступ к download.pytorch.org) и место на диске (~6 ГБ), затем запустите ./install.sh снова."
        fi
    fi
    SUM_TORCH_VER="$(torch_version)"
    [ -n "$SUM_TORCH_VER" ] || fail "PyTorch установлен, но не импортируется." "удалите папку .venv и запустите ./install.sh снова."
fi

# ----------------------------------------------------------------- 5. requirements
if [ "$MOCK" = 1 ]; then
    step 5 "Установка зависимостей интерфейса (requirements.txt)"
    "$VENV_PY" -m pip install -r "$APP_DIR/requirements.txt" ||
        fail "Не удалось установить зависимости." "прочитайте ошибку pip выше и запустите ./install.sh снова."
else
    step 5 "Установка остальных зависимостей (requirements-gpu.txt)"
    # Фиксируем установленный torch, чтобы pip не заменил его другой сборкой.
    constraint="$(mktemp)"
    echo "torch==$SUM_TORCH_VER" >"$constraint"
    "$VENV_PY" -m pip install -r "$APP_DIR/requirements-gpu.txt" -c "$constraint"
    rc=$?
    rm -f "$constraint"
    [ "$rc" = 0 ] || fail "Не удалось установить зависимости." "прочитайте ошибку pip выше и запустите ./install.sh снова."
fi
ok "зависимости установлены"

# ----------------------------------------------------------------- 6. check + models
step 6 "Проверка и папки для моделей"
"$VENV_PY" -c 'import fastapi, uvicorn, PIL, numpy' 2>/dev/null ||
    fail "Не импортируются fastapi/uvicorn/pillow/numpy." "удалите папку .venv и запустите ./install.sh снова."
ok "веб-сервер готов"

if [ "$MOCK" != 1 ]; then
    accel="$("$VENV_PY" -c 'import torch; print("cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu"))' 2>/dev/null)"
    case "$accel" in
        cuda)
            SUM_ACCEL="да (CUDA)"
            ok "PyTorch видит видеокарту NVIDIA"
            ;;
        mps)
            SUM_ACCEL="да (MPS)"
            ok "PyTorch использует Apple MPS"
            ;;
        *)
            warn "PyTorch не видит ускоритель - всё будет считаться на CPU (очень медленно)."
            MISSING+=("ускоритель в PyTorch (работа на CPU)")
            ;;
    esac
fi

mkdir -p "$MODELS_DIR/checkpoints" "$MODELS_DIR/loras"
ok "папки models/checkpoints и models/loras на месте"

if [ "$MOCK" != 1 ]; then
    if ! ls "$MODELS_DIR"/checkpoints/*.safetensors >/dev/null 2>&1; then
        info "В models/checkpoints пока нет .safetensors - модель по умолчанию скачается при первой генерации (~7 ГБ)."
    fi
fi

# ----------------------------------------------------------------- summary
echo
echo "============================================="
echo " Установка завершена"
echo "============================================="
echo "Python:          $SUM_PYTHON"
if [ "$MOCK" = 1 ]; then
    echo "Режим:           mock (только интерфейс), запуск - ./run.sh --mock"
else
    echo "Ускоритель:      $SUM_GPU"
    echo "Видеопамять:     $SUM_VRAM"
    echo "Драйвер:         $SUM_DRIVER"
    echo "PyTorch:         $SUM_TORCH_VER (сборка $SUM_TORCH_TAG)"
    echo "Ускорение:       $SUM_ACCEL"
fi
if [ "${#MISSING[@]}" -gt 0 ]; then
    echo "Не хватает:"
    for m in "${MISSING[@]}"; do echo "  - $m"; done
else
    echo "Всё на месте."
fi
echo
if [ "$MOCK" = 1 ]; then
    echo "Запуск: ./run.sh --mock"
else
    echo "Запуск: ./run.sh"
fi
exit 0
