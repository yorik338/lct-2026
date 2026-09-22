# LCT 2026 Leaderboard MVP

Минимальный каркас для варианта A: участники загружают готовый GeoJSON-результат,
а система валидирует формат, пересчитывает базовые метрики и готовит данные для
публичного лидерборда.

## Быстрый запуск

```powershell
python -m lct_leaderboard.cli validate `
  --catalog "C:\Users\igorv\Downloads\Telegram Desktop\heat_network_datasets_2026-09-21.zip!data/benchmark_specs/rule_catalog.json" `
  --result "C:\Users\igorv\Downloads\Telegram Desktop\heat_network_datasets_2026-09-21.zip!data/benchmark_fixtures/new_tz_smoke/valid_result.geojson"
```

## Веб-борда локально

```powershell
$env:PYTHONPATH="src"
$env:LCT_CATALOG_PATH="C:\Users\igorv\Downloads\Telegram Desktop\heat_network_datasets_2026-09-21.zip!data/benchmark_specs/rule_catalog.json"
$env:LCT_INPUT_PATH="C:\Users\igorv\Downloads\Telegram Desktop\heat_network_datasets_2026-09-21.zip!data/benchmark_fixtures/new_tz_smoke/input.geojson"
$env:LCT_SAMPLE_RESULT_PATH="C:\Users\igorv\Downloads\Telegram Desktop\heat_network_datasets_2026-09-21.zip!data/benchmark_fixtures/new_tz_smoke/valid_result.geojson"
python -m lct_leaderboard.web
```

Открыть:

```text
http://localhost:8000
```

На странице доступны скачивания:

- `/download/input` - входной датасет;
- `/download/catalog` - каталог правил и ставок;
- `/download/sample-result` - пример валидного результата, если задан `LCT_SAMPLE_RESULT_PATH`.

## Публичный демо-доступ через Cloudflare Tunnel

После запуска локальной борды:

```powershell
cloudflared tunnel --url http://localhost:8000
```

Cloudflare выдаст временный публичный URL. Это самый быстрый бесплатный способ
показать демо без деплоя и без открытия входящих портов.

## Render Free

В репозитории есть `render.yaml`, а минимальные демо-файлы лежат в `demo_data/`.
После пуша в GitHub можно создать Render Blueprint или Web Service из этого
репозитория. Сервис стартует командой:

```text
python -m lct_leaderboard.web
```

Быстрый ручной деплой:

1. Открыть Render Dashboard.
2. Нажать `New` -> `Blueprint`.
3. Выбрать репозиторий `baddboy40/lct-2026`.
4. Если Render спросит ветку, выбрать `develop`.
5. Подтвердить создание сервиса `lct-leaderboard`.

Если создавать не Blueprint, а обычный `Web Service`, параметры такие:

- Branch: `develop`;
- Runtime: `Python`;
- Build Command: `pip install -r requirements.txt`;
- Start Command: `PYTHONPATH=src python -m lct_leaderboard.web`;
- Env:
  - `PYTHONPATH=src`;
  - `PYTHON_VERSION=3.12.8`;
  - `LCT_DATA_DIR=data`;
  - `LCT_CATALOG_PATH=demo_data/rule_catalog.json`;
  - `LCT_INPUT_PATH=demo_data/input.geojson`;
  - `LCT_SAMPLE_RESULT_PATH=demo_data/valid_result.geojson`.
  - `LCT_MAX_SUBMISSIONS_PER_TEAM_DATASET=3`;
  - `LCT_SUBMISSIONS_CLOSE_AT=2026-09-25T23:59:00Z`.

Render Free подходит для демо, но не для постоянной боевой борды без внешней БД:
free web service засыпает после простоя, а локальная файловая система может
очищаться при рестартах/редеплоях.

### Keepalive на 3 дня

Чтобы демо не засыпало во время короткого публичного показа, добавлен GitHub
Actions workflow `.github/workflows/keep-render-awake.yml`. Он дергает
`/healthz` каждые 10 минут до `2026-09-25T23:59:00Z`.

Если Render выдал URL не `https://lct-leaderboard.onrender.com`, в GitHub нужно
создать repository variable:

```text
LEADERBOARD_URL=https://your-render-url.onrender.com
```

Если нужно продлить окно, поменять:

```text
KEEPALIVE_UNTIL=2026-09-25T23:59:00Z
```

Импорт manifest из архива:

```powershell
python -m lct_leaderboard.cli import-scenes `
  --manifest "C:\Users\igorv\Downloads\Telegram Desktop\heat_network_datasets_2026-09-21.zip!data/rl_large_smoke/scene_manifest.json" `
  --out datasets.json
```

Поддерживаются обычные пути и zip-пути вида:

```text
archive.zip!path/inside/archive.json
```

## Что уже проверяет basic-верификатор

- GeoJSON `FeatureCollection`;
- обязательные поля `heat_network`, `heat_chamber`, `variant_summary`;
- уникальность `properties.id`;
- конечность чисел;
- наличие диаметров в `pipe_catalog`;
- пропускную способность трубы по `capacity_tph`;
- пересчет длины LineString;
- базовую стоимость новых участков;
- стоимость камер;
- штраф за неподключенные ОКС;
- расхождения с заявленным `variant_summary`.

Полные инженерные проверки геометрии будут добавлены отдельным слоем.

## Боевой минимум

В текущей версии включены:

- лимит 3 сабмита на команду и датасет;
- deadline через `LCT_SUBMISSIONS_CLOSE_AT`;
- скрытие локальных путей из публичного UI;
- экспорт лидерборда в CSV;
- JSON-отчет по каждому сабмиту;
- health-check `/healthz` для Render;
- GitHub Actions keepalive на 3 дня.
