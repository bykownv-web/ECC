# FaceSwap Studio

Локальное веб-приложение для генерации изображений по тексту и подмены лиц.
Все модели работают на вашем компьютере — никакие фото не уходят во внешние сервисы.

- **Генерация** — text-to-image через [diffusers](https://github.com/huggingface/diffusers) (по умолчанию SDXL, подходит любая совместимая модель: SD 1.5, SDXL, SDXL-Turbo, RealVisXL и т.д.).
- **Подмена лица** — детекция InsightFace (`buffalo_l`) + модель `inswapper_128.onnx`.
- **Генерация + лицо** — сгенерировать сцену и сразу вставить в неё лицо с фото.

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
Пак детекции `buffalo_l` InsightFace скачает сам при первом запуске (в `~/.insightface`).

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
| `FSS_FACE_PACK` | `buffalo_l` | Пак детекции InsightFace |
| `FSS_NSFW_MODEL` | `Falconsai/nsfw_image_detection` | NSFW-классификатор (пустая строка — выключить) |
| `FSS_NSFW_THRESHOLD` | `0.7` | Порог NSFW-классификатора |
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
  backends.py  — DiffusersGenerator (HF / .safetensors + LoRA), InsightFaceSwapper, mock
  registry.py  — поиск локальных моделей и безопасное разрешение имён файлов
  safety.py    — фильтр промптов, NSFW-проверка, водяной знак, метаданные
  config.py    — настройки из переменных окружения
  static/      — веб-интерфейс (HTML/CSS/JS без сборки)
tests/         — тесты API на mock-бэкенде
```

## Тесты

```bash
pip install -r requirements-dev.txt
python -m pytest
ruff check .
```

Тесты не требуют GPU и весов моделей — используется `FSS_BACKEND=mock`.
