# Image Studio - установщик для Windows (PowerShell 5.1+).
# Обычно запускается через install.bat двойным щелчком.
#
#   install.bat                  - полная установка (PyTorch с CUDA, если есть видеокарта NVIDIA)
#   install.bat -Mock            - только веб-интерфейс без моделей (FSS_BACKEND=mock)
#   install.bat -Torch cu121     - принудительно выбрать сборку PyTorch: cu128, cu124, cu121 или cpu
#
# Скрипт можно запускать повторно: уже сделанные шаги пропускаются.

[CmdletBinding()]
param(
    [switch]$Mock,
    [ValidateSet('auto', 'cu128', 'cu124', 'cu121', 'cpu')]
    [string]$Torch = 'auto'
)

# Ошибки внешних программ проверяем сами через $LASTEXITCODE.
# 'Stop' в PS 5.1 превращает любые предупреждения pip в stderr в аварийную остановку.
$ErrorActionPreference = 'Continue'

$AppDir = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $AppDir '.venv'
$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'
$ModelsDir = Join-Path $AppDir 'models'
$TorchIndexBase = 'https://download.pytorch.org/whl/'
$PythonDownload = 'https://www.python.org/downloads/release/python-3119/'
$Total = 6

# Итоги для сводки в конце
$Summary = [ordered]@{
    Python    = '-'
    Gpu       = 'не найдена'
    Vram      = '-'
    Driver    = '-'
    TorchTag  = '-'
    TorchVer  = '-'
    TorchCuda = 'нет'
}
$Missing = New-Object System.Collections.Generic.List[string]

function Write-Step([int]$Number, [string]$Text) {
    Write-Host ''
    Write-Host ('[{0}/{1}] {2}' -f $Number, $Total, $Text) -ForegroundColor Cyan
}

function Write-Ok([string]$Text) {
    Write-Host ('      OK: ' + $Text) -ForegroundColor Green
}

function Write-Info([string]$Text) {
    Write-Host ('      ' + $Text)
}

function Write-Warn([string]$Text) {
    Write-Host ('      ВНИМАНИЕ: ' + $Text) -ForegroundColor Yellow
}

function Stop-Install([string]$What, [string]$Hint) {
    Write-Host ''
    Write-Host ('[ОШИБКА] ' + $What) -ForegroundColor Red
    if ($Hint) {
        Write-Host ('Подсказка: ' + $Hint) -ForegroundColor Yellow
    }
    Write-Host 'Установка прервана. Исправьте проблему и снова запустите install.bat.' -ForegroundColor Red
    exit 1
}

function Get-PythonVersion([string]$Exe, [string[]]$PrefixArgs) {
    # Возвращает [version] или $null, если такого Python нет / он не запускается.
    $out = $null
    try {
        $out = & $Exe @PrefixArgs -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0 -or -not $out) {
        return $null
    }
    $text = ([string]($out | Select-Object -First 1)).Trim()
    try {
        return [version]$text
    } catch {
        return $null
    }
}

function Test-SupportedPython([version]$Version) {
    return ($Version.Major -eq 3 -and $Version.Minor -ge 10 -and $Version.Minor -le 12)
}

function Get-TorchVersion {
    $out = & $VenvPy -c "import torch; print(torch.__version__)" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $out) {
        return $null
    }
    return ([string]($out | Select-Object -First 1)).Trim()
}

function Get-TorchTag([string]$Version) {
    # '2.6.0+cu124' -> 'cu124'; без суффикса (сборка с PyPI) -> 'pypi'
    if (-not $Version) {
        return $null
    }
    $parts = $Version.Split('+')
    if ($parts.Count -lt 2) {
        return 'pypi'
    }
    return $parts[1]
}

# pip вызываем прямо из тела скрипта, а не из функции: так PowerShell не перехватывает
# его вывод и в окне виден прогресс скачивания.

Write-Host '=============================================' -ForegroundColor Cyan
Write-Host ' Image Studio - установка' -ForegroundColor Cyan
Write-Host '=============================================' -ForegroundColor Cyan
Write-Host ('Папка приложения: ' + $AppDir)
if ($Mock) {
    Write-Host 'Режим -Mock: только интерфейс, без PyTorch и моделей.' -ForegroundColor Yellow
}

# ----------------------------------------------------------------- 1. Python
Write-Step 1 'Поиск Python 3.10-3.12'

$candidates = @(
    @{ Exe = 'py'; Args = @('-3.12') },
    @{ Exe = 'py'; Args = @('-3.11') },
    @{ Exe = 'py'; Args = @('-3.10') },
    @{ Exe = 'python'; Args = @() },
    @{ Exe = 'python3'; Args = @() }
)
foreach ($minor in @('312', '311', '310')) {
    $candidates += @{ Exe = (Join-Path $env:LOCALAPPDATA ('Programs\Python\Python' + $minor + '\python.exe')); Args = @() }
    $candidates += @{ Exe = (Join-Path $env:ProgramFiles ('Python' + $minor + '\python.exe')); Args = @() }
}

$PyExe = $null
$PyArgs = @()
$PyVersion = $null
$tooNew = $null
$tooOld = $null

foreach ($c in $candidates) {
    $isPath = $c.Exe.Contains('\')
    if ($isPath) {
        if (-not (Test-Path -LiteralPath $c.Exe)) {
            continue
        }
    } elseif (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) {
        continue
    }
    $v = Get-PythonVersion $c.Exe $c.Args
    if (-not $v) {
        continue
    }
    if (Test-SupportedPython $v) {
        $PyExe = $c.Exe
        $PyArgs = $c.Args
        $PyVersion = $v
        break
    }
    if ($v.Major -eq 3 -and $v.Minor -ge 13) {
        $tooNew = $v
    } else {
        $tooOld = $v
    }
}

if (-not $PyExe) {
    if ($tooNew) {
        Stop-Install ('Найден только Python ' + $tooNew + '. Он слишком новый: для 3.13+ пока нет надёжных сборок PyTorch.') ('установите Python 3.11 (Windows installer 64-bit): ' + $PythonDownload + ' . При установке отметьте "Add python.exe to PATH". Старый Python удалять не нужно.')
    }
    if ($tooOld) {
        Stop-Install ('Найден только Python ' + $tooOld + '. Нужен Python 3.10, 3.11 или 3.12.') ('установите Python 3.11 (Windows installer 64-bit): ' + $PythonDownload + ' . При установке отметьте "Add python.exe to PATH".')
    }
    Stop-Install 'Python не найден.' ('установите Python 3.11 (Windows installer 64-bit): ' + $PythonDownload + ' . При установке отметьте "Add python.exe to PATH", затем закройте и снова откройте окно.')
}

$Summary.Python = [string]$PyVersion
$pyLabel = (@($PyExe) + $PyArgs) -join ' '
Write-Ok ('Python ' + $PyVersion + ' (' + $pyLabel + ')')

# ----------------------------------------------------------------- 2. venv + pip
Write-Step 2 'Виртуальное окружение .venv и обновление pip'

if (Test-Path -LiteralPath $VenvPy) {
    $venvVersion = Get-PythonVersion $VenvPy @()
    if (-not $venvVersion) {
        Stop-Install ('Папка .venv повреждена: ' + $VenvPy + ' не запускается.') ('удалите папку ' + $VenvDir + ' и снова запустите install.bat.')
    }
    if (-not (Test-SupportedPython $venvVersion)) {
        Stop-Install ('В .venv стоит Python ' + $venvVersion + ', а нужен 3.10-3.12.') ('удалите папку ' + $VenvDir + ' и снова запустите install.bat.')
    }
    Write-Ok ('.venv уже есть (Python ' + $venvVersion + ')')
} else {
    if (Test-Path -LiteralPath $VenvDir) {
        Stop-Install ('Папка .venv есть, но в ней нет Scripts\python.exe.') ('удалите папку ' + $VenvDir + ' и снова запустите install.bat.')
    }
    Write-Info 'Создаю .venv ...'
    & $PyExe @PyArgs -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPy)) {
        Stop-Install 'Не удалось создать виртуальное окружение .venv.' 'переустановите Python с python.org (с галочкой "pip" и "py launcher") и попробуйте снова.'
    }
    Write-Ok '.venv создано'
}

Write-Info 'Обновляю pip (старый pip ломается на установке PyTorch) ...'
& $VenvPy -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Stop-Install 'Не удалось обновить pip.' 'проверьте подключение к интернету, прокси и антивирус, затем запустите install.bat снова.'
}
Write-Ok 'pip обновлён'

# ----------------------------------------------------------------- 3. GPU
Write-Step 3 'Поиск видеокарты NVIDIA'

$TorchTag = 'cpu'
if ($Mock) {
    Write-Info 'Пропущено (-Mock).'
} else {
    $smi = $null
    $smiCmd = Get-Command 'nvidia-smi' -ErrorAction SilentlyContinue
    if ($smiCmd) {
        $smi = $smiCmd.Path
    } else {
        foreach ($p in @((Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'), (Join-Path $env:ProgramFiles 'NVIDIA Corporation\NVSMI\nvidia-smi.exe'))) {
            if (Test-Path -LiteralPath $p) {
                $smi = $p
                break
            }
        }
    }

    $driver = $null
    if (-not $smi) {
        Write-Warn 'nvidia-smi не найден: видеокарты NVIDIA нет или не установлен драйвер.'
        Write-Warn 'Будет установлен PyTorch для CPU - генерация будет ОЧЕНЬ медленной.'
        Write-Warn 'Если видеокарта NVIDIA есть, установите драйвер: https://www.nvidia.com/Download/index.aspx и запустите install.bat снова.'
    } else {
        $lines = & $smi '--query-gpu=name,memory.total,driver_version' '--format=csv,noheader' 2>$null
        $first = $null
        if ($LASTEXITCODE -eq 0 -and $lines) {
            $first = @($lines | Where-Object { $_ -and $_.Trim() }) | Select-Object -First 1
        }
        if (-not $first) {
            Write-Warn 'nvidia-smi есть, но не ответил. Возможно, драйвер NVIDIA повреждён - переустановите его.'
            Write-Warn 'Пока будет установлен PyTorch для CPU.'
        } else {
            $parts = ([string]$first).Split(',')
            $Summary.Gpu = $parts[0].Trim()
            if ($parts.Count -ge 2) {
                $Summary.Vram = $parts[1].Trim()
            }
            if ($parts.Count -ge 3) {
                $Summary.Driver = $parts[2].Trim()
                try {
                    $driver = [version]$Summary.Driver
                } catch {
                    $driver = $null
                }
            }
            Write-Ok ('{0}, {1}, драйвер {2}' -f $Summary.Gpu, $Summary.Vram, $Summary.Driver)
            $mib = 0
            if ($Summary.Vram -match '(\d+)') {
                $mib = [int]$Matches[1]
            }
            if ($mib -gt 0 -and $mib -lt 7500) {
                Write-Warn 'Меньше 8 ГБ видеопамяти: SDXL может не поместиться, берите модели SD 1.5 и размер 512-768.'
            }
        }
    }

    if ($driver) {
        if ($driver -ge [version]'572.61') {
            $TorchTag = 'cu128'
        } elseif ($driver -ge [version]'551.0') {
            $TorchTag = 'cu124'
        } elseif ($driver -ge [version]'531.0') {
            $TorchTag = 'cu121'
        } else {
            Write-Warn ('Драйвер ' + $Summary.Driver + ' слишком старый для CUDA 12 (нужен 531+). Будет установлен PyTorch для CPU.')
            Write-Warn 'Обновите драйвер: https://www.nvidia.com/Download/index.aspx и запустите install.bat снова.'
        }
    }

    if ($Torch -ne 'auto') {
        Write-Info ('Сборка PyTorch выбрана вручную: ' + $Torch)
        $TorchTag = $Torch
    }
    Write-Info ('Сборка PyTorch: ' + $TorchTag + ' (' + $TorchIndexBase + $TorchTag + ')')
}

# ----------------------------------------------------------------- 4. torch
Write-Step 4 'Установка PyTorch'

if ($Mock) {
    Write-Info 'Пропущено (-Mock).'
} else {
    $Summary.TorchTag = $TorchTag
    $installed = Get-TorchVersion
    $installedTag = Get-TorchTag $installed
    if ($installed -and $installedTag -eq $TorchTag) {
        Write-Ok ('PyTorch ' + $installed + ' уже установлен')
    } else {
        if ($installed) {
            Write-Info ('Сейчас стоит PyTorch ' + $installed + ', нужна сборка ' + $TorchTag + '. Переустанавливаю ...')
            & $VenvPy -m pip uninstall -y torch torchvision torchaudio
        }
        Write-Info 'Скачивание может занять 5-20 минут (файл до 3 ГБ) ...'
        # Только --index-url: если добавить PyPI, pip возьмёт оттуда torch без CUDA.
        & $VenvPy -m pip install torch --index-url ($TorchIndexBase + $TorchTag) --timeout 120
        if ($LASTEXITCODE -ne 0) {
            Stop-Install ('Не удалось установить PyTorch (' + $TorchTag + ').') 'проверьте интернет (нужен доступ к download.pytorch.org) и свободное место на диске (~6 ГБ), затем запустите install.bat снова. Если ошибка повторяется на видеокарте NVIDIA, попробуйте install.bat -Torch cu121.'
        }
    }
    $Summary.TorchVer = Get-TorchVersion
    if (-not $Summary.TorchVer) {
        Stop-Install 'PyTorch установлен, но не импортируется.' 'удалите папку .venv и запустите install.bat снова.'
    }
}

# ----------------------------------------------------------------- 5. requirements
if ($Mock) {
    Write-Step 5 'Установка зависимостей интерфейса (requirements.txt)'
    & $VenvPy -m pip install -r (Join-Path $AppDir 'requirements.txt')
} else {
    Write-Step 5 'Установка остальных зависимостей (requirements-gpu.txt)'
    # Фиксируем уже установленный torch, чтобы pip не заменил его сборкой без CUDA с PyPI.
    $constraint = Join-Path $env:TEMP 'fss-torch-constraint.txt'
    Set-Content -LiteralPath $constraint -Value ('torch==' + $Summary.TorchVer) -Encoding ASCII
    & $VenvPy -m pip install -r (Join-Path $AppDir 'requirements-gpu.txt') -c $constraint
}
if ($LASTEXITCODE -ne 0) {
    Stop-Install 'Не удалось установить зависимости.' 'прочитайте последние строки ошибки pip выше; часто помогает повторный запуск install.bat. Если pip пишет про конфликт версий torch - сообщите об этом разработчику.'
}
Write-Ok 'зависимости установлены'

# ----------------------------------------------------------------- 6. check + models
Write-Step 6 'Проверка и папки для моделей'

& $VenvPy -c "import fastapi, uvicorn, PIL, numpy" 2>$null
if ($LASTEXITCODE -ne 0) {
    Stop-Install 'Не импортируются fastapi/uvicorn/pillow/numpy.' 'удалите папку .venv и запустите install.bat снова.'
}
Write-Ok 'веб-сервер готов'

if (-not $Mock) {
    $check = & $VenvPy -c "import torch; print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-')" 2>$null
    $cudaLine = ''
    if ($check) {
        $cudaLine = ([string](@($check)[0])).Trim()
    }
    if ($cudaLine -eq 'True') {
        $Summary.TorchCuda = 'да'
        Write-Ok ('PyTorch видит видеокарту: ' + ([string](@($check)[1])).Trim())
    } else {
        Write-Warn 'PyTorch не видит видеокарту - всё будет считаться на CPU (очень медленно).'
        if ($TorchTag -ne 'cpu') {
            Write-Warn 'Обновите драйвер NVIDIA и перезагрузите компьютер, затем запустите install.bat снова.'
        }
        $Missing.Add('CUDA в PyTorch (работа на CPU)')
    }
}

foreach ($d in @($ModelsDir, (Join-Path $ModelsDir 'checkpoints'), (Join-Path $ModelsDir 'loras'))) {
    if (-not (Test-Path -LiteralPath $d)) {
        [void](New-Item -ItemType Directory -Path $d -Force)
    }
}
Write-Ok 'папки models\checkpoints и models\loras на месте'

if (-not $Mock) {
    $ckpt = @(Get-ChildItem -LiteralPath (Join-Path $ModelsDir 'checkpoints') -Filter '*.safetensors' -ErrorAction SilentlyContinue)
    if ($ckpt.Count -eq 0) {
        Write-Info 'В models\checkpoints пока нет .safetensors - модель по умолчанию скачается при первой генерации (~7 ГБ).'
    } else {
        Write-Ok ('чекпоинтов в models\checkpoints: ' + $ckpt.Count)
    }
}

# ----------------------------------------------------------------- summary
Write-Host ''
Write-Host '=============================================' -ForegroundColor Green
Write-Host ' Установка завершена' -ForegroundColor Green
Write-Host '=============================================' -ForegroundColor Green
Write-Host ('Python:          ' + $Summary.Python)
if ($Mock) {
    Write-Host 'Режим:           mock (только интерфейс), запуск - run-mock.bat'
} else {
    Write-Host ('Видеокарта:      ' + $Summary.Gpu)
    Write-Host ('Видеопамять:     ' + $Summary.Vram)
    Write-Host ('Драйвер:         ' + $Summary.Driver)
    Write-Host ('PyTorch:         ' + $Summary.TorchVer + ' (сборка ' + $Summary.TorchTag + ')')
    Write-Host ('CUDA в PyTorch:  ' + $Summary.TorchCuda)
}
if ($Missing.Count -gt 0) {
    Write-Host 'Не хватает:' -ForegroundColor Yellow
    foreach ($m in $Missing) {
        Write-Host ('  - ' + $m) -ForegroundColor Yellow
    }
} else {
    Write-Host 'Всё на месте.' -ForegroundColor Green
}
Write-Host ''
if ($Mock) {
    Write-Host 'Запуск: дважды щёлкните run-mock.bat' -ForegroundColor Cyan
} else {
    Write-Host 'Запуск: дважды щёлкните run.bat' -ForegroundColor Cyan
}
exit 0
