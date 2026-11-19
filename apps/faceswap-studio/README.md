# FaceSwap Studio

Локальное веб-приложение для генерации изображений по тексту и подмены лиц.
Все модели работают на вашем компьютере — никакие фото не уходят во внешние сервисы.

- **Генерация** — text-to-image через [diffusers](https://github.com/huggingface/diffusers) (по умолчанию SDXL, подходит любая совместимая модель: SD 1.5, SDXL, SDXL-Turbo, RealVisXL и т.д.).
- **Подмена лица** — модели InsightFace (`buffalo_l` + `inswapper_128.onnx`), запускаются напрямую через onnxruntime: пакет `insightface` и Visual C++ Build Tools не нужны.
- **Прогресс** — полоска с шагами генерации, очередь задач и кнопка «Отменить».
- **Генерация + лицо** — сгенерировать сцену и сразу вставить в неё лицо с фото.

## Быстрый старт для Windows

Командную строку открывать не нужно — всё делается двойным щелчком.

1. Установите [Python 3.11](https://www.python.org/downloads/release/python-3119/) (Windows installer 64-bit),
   при установке отметьте **Add python.exe to PATH**. Подойдут 3.10–3.12; 3.13 и новее пока не поддерживаются.
2. Скачайте проект: на GitHub кнопка **Code → Download ZIP**, распакуйте архив
   (или `git clone`, если пользуетесь git). Откройте папку `apps\faceswap-studio`.
3. Дважды щёлкните **`install.bat`**. Скрипт сам найдёт Python, создаст окружение `.venv`, обновит pip,
   определит видеокарту NVIDIA и поставит подходящий PyTorch с CUDA. Первый раз это занимает 10–30 минут
   (скачивается несколько ГБ). В конце будет сводка: какая видеокарта, видит ли её PyTorch и чего не хватает.
   Скрипт можно запускать повторно — готовые шаги пропускаются.
4. Положите модели (см. ниже [«Модель подмены лица»](#модель-подмены-лица) и [«Свои модели и LoRA»](#свои-модели-и-lora-civitai-и-др)):
   - `models\inswapper_128.onnx` — для подмены лица (скрипт его не скачивает);
   - `models\checkpoints\*.safetensors` и `models\loras\*.safetensors` — по желанию.
5. Дважды щёлкните **`run.bat`** — браузер откроет <http://127.0.0.1:8000>, когда сервер будет готов.
   Чтобы остановить приложение, закройте чёрное окно.

Без видеокарты и моделей можно посмотреть интерфейс: `install.bat -Mock`, затем `run-mock.bat`.
На Linux и macOS то же самое делают `./install.sh` и `./run.sh` (на macOS используется MPS).

### Если что-то пошло не так

| Что видите | Что делать |
|---|---|
| «Выполнение сценариев отключено в этой системе» (ExecutionPolicy) | Запускайте `install.bat`, а не `install.ps1` и не `Activate.ps1`: bat-файл сам обходит это ограничение только для установщика. |
| «Python не найден» или «слишком новый» | Установите Python 3.11 по ссылке выше с галочкой **Add python.exe to PATH**, закройте окно и снова запустите `install.bat`. |
| Ошибка pip про `typing_extensions` или «No matching distribution found for torch» | Это старый pip. `install.bat` обновляет его сам; если ошибка осталась — проверьте интернет и запустите ещё раз. |
| В сводке «CUDA в PyTorch: нет», генерация очень медленная | Стоит PyTorch для CPU. Обновите драйвер NVIDIA, перезагрузите компьютер и снова запустите `install.bat` — он сам заменит PyTorch. Не ставьте `pip install torch` вручную: с PyPI на Windows ставится версия без CUDA. |
| «nvidia-smi не найден» | Видеокарты NVIDIA нет или не установлен драйвер. Установите драйвер с [nvidia.com](https://www.nvidia.com/Download/index.aspx). Без NVIDIA всё работает только на CPU. |
| `CUDA out of memory` / нехватка памяти | Уменьшите размер картинки (768×768 или 512×512) и число шагов, закройте игры и другие программы на видеокарте. На картах до 10 ГБ режим экономии памяти включается сам; если не помогает — задайте `FSS_LOWVRAM=on` или берите модели SD 1.5. |
| PyTorch не ставится с автоматически выбранной сборкой | Укажите сборку вручную: `install.bat -Torch cu121` (или `cu124`, `cu128`, `cpu`). |
| `run.bat` пишет «сначала запустите install.bat» | Установка не была завершена — запустите `install.bat` и дочитайте сводку в конце. |

## Ответственное использование

В приложение встроены защитные меры. Не отключайте их без веской причины:

- для подмены лица обязательна галочка о согласии человека, чьё лицо используется;
- на каждый результат ставится видимая метка `AI-generated`, а в PNG пишутся метаданные `ai_generated=true`;
- промпты с сексуальным контентом и упоминанием несовершеннолетних отклоняются;
- результаты проверяются NSFW-классификатором (`Falconsai/nsfw_image_detection`).

Запрещено использовать приложение для интимных изображений без согласия, мошенничества,
дезинформации, травли и выдачи себя за другого человека. Во многих странах это уголовно наказуемо.

## Требования

- Python 3.10+
- Для реальных моделей: GPU NVIDIA с 8+ ГБ VRAM (SDXL) или Apple Silicon. На CPU работает, но очень медленно.
- ~10 ГБ на диске под веса моделей.

## Установка

На Windows проще всего использовать `install.bat` (см. [«Быстрый старт для Windows»](#быстрый-старт-для-windows)).
Ручная установка:

```bash
cd apps/faceswap-studio
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 1. PyTorch под вашу платформу (пример для CUDA 12.4):
pip install torch --index-url https://download.pytorch.org/whl/cu124

# 2. Остальные зависимости
pip install -r requirements-gpu.txt
```

### Модель подмены лица

Файл `inswapper_128.onnx` (~530 МБ) не скачивается автоматически. Положите его в
`models/inswapper_128.onnx` или укажите путь через `FSS_SWAP_MODEL_PATH`.
Модели InsightFace (`inswapper_128`, `buffalo_l`) распространяются **только для некоммерческого
исследовательского использования** — проверьте лицензию перед применением.
Детектор и распознаватель лиц (`det_10g.onnx`, `w600k_r50.onnx` из официального архива `buffalo_l`,
~280 МБ) скачиваются сами при первой подмене лица в `models/buffalo_l/`. Если у вас уже есть
`~/.insightface/models/buffalo_l`, используется он. Если скачивание не удалось, скачайте вручную
<https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip> и распакуйте эти
два файла в `models/buffalo_l/`.

Подмена лица работает на видеокарте NVIDIA через `onnxruntime-gpu`. Для AMD/Intel на Windows
поставьте `onnxruntime-directml` вместо `onnxruntime-gpu`. Не ставьте `onnxruntime` и
`onnxruntime-gpu` одновременно.

Модель генерации скачивается с Hugging Face при первом запросе (в `~/.cache/huggingface`).

### Свои модели и LoRA (Civitai и др.)

Файлы `.safetensors` кладутся в две папки, приложение находит их само (перезапуск не нужен):

```text
models/
  checkpoints/   ← полные модели, 2–7 ГБ (например juggernautXL_ragnarok.safetensors)
  loras/         ← LoRA, обычно 10–300 МБ (например edgCrystalButterflyv1.safetensors)
```

В интерфейсе появится выпадающий список «Модель» и кнопка «+ LoRA» (до 5 штук, вес от −2 до 2).

- Поддерживаются чекпоинты **SDXL** (Juggernaut XL, Illustrious, Pony, RealVisXL…) и **SD 1.5**. Архитектура определяется автоматически.
- LoRA должна быть обучена под ту же базу, что и выбранная модель: LoRA для SDXL не работает с SD 1.5 и наоборот. Базу видно на странице модели на Civitai («Base Model»).
- Многие LoRA срабатывают только со своими trigger words, их тоже берите со страницы модели и добавляйте в промпт.
- Переключение чекпоинта перезагружает модель (10–60 с). Одновременно в памяти держится одна модель.
- Пути к папкам можно поменять через `FSS_CHECKPOINTS_DIR` и `FSS_LORAS_DIR`.

Для чекпоинтов Illustrious обычно лучше CFG 4–6 и около 25–30 шагов, для Juggernaut XL CFG 4–7 и 30–40 шагов.
Фильтры контента и метка `AI-generated` действуют для любых моделей.

## Запуск

```bash
python -m app.main                      # http://127.0.0.1:8000
python -m app.main --host 0.0.0.0 --port 8080
```

Без GPU и весов (для разработки интерфейса) можно запустить заглушку:

```bash
pip install -r requirements.txt
FSS_BACKEND=mock python -m app.main
```

## Настройка (переменные окружения)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `FSS_BACKEND` | `diffusers` | `diffusers` — реальные модели, `mock` — заглушка |
| `FSS_MODEL_ID` | `stabilityai/stable-diffusion-xl-base-1.0` | Модель генерации (id на HF или локальный путь) |
| `FSS_DEVICE` | `auto` | `cuda`, `mps`, `cpu` или `auto` |
| `FSS_CHECKPOINTS_DIR` | `models/checkpoints/` | Папка с локальными чекпоинтами `.safetensors` |
| `FSS_LORAS_DIR` | `models/loras/` | Папка с LoRA `.safetensors` |
| `FSS_SWAP_MODEL_PATH` | `models/inswapper_128.onnx` | Путь к модели подмены лица |
| `FSS_NSFW_MODEL` | `Falconsai/nsfw_image_detection` | NSFW-классификатор (пустая строка — выключить) |
| `FSS_NSFW_THRESHOLD` | `0.7` | Порог NSFW-классификатора |
| `FSS_LOWVRAM` | `auto` | Экономия видеопамяти (CPU offload + VAE tiling): `auto` включает при < 10 ГБ, `on`, `off` |
| `FSS_MOCK_DELAY` | `0` | Секунд на «шаг» в mock-режиме (чтобы увидеть полоску прогресса) |
| `FSS_WATERMARK` | `1` | Видимая метка `AI-generated` |
| `FSS_OUTPUT_DIR` | `outputs/` | Куда сохраняются результаты |
| `FSS_MAX_UPLOAD_MB` | `10` | Максимальный размер загружаемого файла |
| `FSS_MAX_SIDE` | `1536` | Максимальная сторона изображения |
| `FSS_DEFAULT_STEPS` / `FSS_MAX_STEPS` | `30` / `80` | Шаги диффузии |
| `FSS_BLOCKED_TERMS` | — | Дополнительные запрещённые слова через запятую |

Пример быстрой генерации (4 шага): `FSS_MODEL_ID=stabilityai/sdxl-turbo FSS_DEFAULT_STEPS=4`
(на вкладке генерации поставьте CFG = 0).

## API

| Метод | Путь | Тело |
|---|---|---|
| `GET` | `/api/health` | — |
| `GET` | `/api/models` | — (список чекпоинтов и LoRA) |
| `POST` | `/api/generate` | JSON: `prompt`, `negative_prompt`, `width`, `height`, `steps`, `guidance`, `seed`, `model`, `loras: [{name, weight}]` |
| `POST` | `/api/swap` | multipart: `source`, `target`, `consent=true`, `all_faces` |
| `POST` | `/api/generate-swap` | multipart: `source`, `prompt`, `consent=true`, + параметры генерации, `loras` как JSON-строка |
| `GET` | `/api/images/{id}` | — (PNG) |
| `DELETE` | `/api/images/{id}` | — |
| `POST` | `/api/jobs/generate`, `/api/jobs/swap`, `/api/jobs/generate-swap` | как у обычных эндпоинтов; ответ `202 {"job_id": ...}` |
| `GET` | `/api/jobs/{job_id}` | — (`status`, `stage`, `progress` 0..1, `queue_position`, `result`, `error`) |
| `POST` | `/api/jobs/{job_id}/cancel` | — |

Задачи выполняются по одной (видеокарта одна) и хранятся в памяти: последние 100, до перезапуска.
`status`: `queued`, `running`, `done` или `error` (код `499` значит «отменено»).
Обычные эндпоинты (`/api/generate` и т.д.) тоже работают и стоят в той же очереди.

```bash
curl -X POST localhost:8000/api/generate -H 'Content-Type: application/json' \
  -d '{"prompt": "lighthouse at dusk, oil painting", "seed": 1}'

curl -X POST localhost:8000/api/swap \
  -F source=@me.jpg -F target=@scene.jpg -F consent=true
```

## Структура

```text
app/
  main.py      — FastAPI: маршруты, загрузка файлов, сохранение результатов
  jobs.py      — очередь задач с прогрессом и отменой
  backends.py  — DiffusersGenerator (HF / .safetensors + LoRA, low-VRAM), InsightFaceSwapper, mock
  face_engine.py — детекция, распознавание и подмена лица на onnxruntime
  registry.py  — поиск локальных моделей и безопасное разрешение имён файлов
  safety.py    — фильтр промптов, NSFW-проверка, водяной знак, метаданные
  config.py    — настройки из переменных окружения
  static/      — веб-интерфейс (HTML/CSS/JS без сборки)
tests/         — тесты на mock-бэкенде и поддельных моделях (без GPU)
```

## Тесты

```bash
pip install -r requirements-dev.txt
python -m pytest
ruff check .
```

Тесты не требуют GPU и весов моделей — используется `FSS_BACKEND=mock`.
