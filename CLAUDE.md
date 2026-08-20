# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`wordstat` — CLI, экспортирующий CSV-отчёты из веб-интерфейса Яндекс Вордстат
через уже открытый и авторизованный Chrome (подключение по CDP). Инструмент
не логинится сам и не хранит учётные данные — он падает, если Wordstat
показывает страницу входа.

## Setup

```bash
cd /Users/axisrow/Projects/wordstat
uv venv --python 3.11
source .venv/bin/activate
uv sync --all-groups
```

`browser-use` подтягивается как editable-зависимость из соседнего каталога
`../browser-use` (см. `[tool.uv.sources]` в `pyproject.toml`). Он должен
существовать рядом с этим репозиторием, иначе `uv sync` упадёт.

Chrome должен быть запущен с доступным CDP endpoint (по умолчанию
`http://127.0.0.1:9222`), заранее авторизован в Яндексе.

## Commands

```bash
# запуск CLI
wordstat collect "ремонт квартир" --region "Москва" --output-dir ./wordstat-output
# --cdp-url / WORDSTAT_CDP_URL, --timeout (сек, по умолчанию 45)

# тесты
pytest
pytest tests/test_csv_io.py::test_parse_wordstat_csv_rejects_duplicate_headers  # один тест

# линт
ruff check .
```

## Architecture

Пайплайн `cli.py` → `collector.py` → (`csv_io.py` + `storage.py`), с
Pydantic-моделями (`models.py`) как контрактом между слоями и доменными
исключениями (`errors.py`), которые CLI превращает в `click.ClickException`.

- **`collector.py` (`WordstatCollector.collect`)** — единственное место, где
  происходит взаимодействие с браузером через `browser_use.BrowserSession`
  (CDP-подключение к уже запущенному Chrome, `keep_alive=True`,
  `allowed_domains` ограничены доменами Wordstat/Passport). Последовательность
  жёстко детерминирована: открыть Wordstat → проверить авторизацию
  (`_assert_authenticated`, ищет ссылку «Выйти», иначе кидает
  `AuthenticationRequiredError`) → ввести фразу → выбрать регион → для каждого
  из четырёх видов отчёта (`WordstatView`) переключить вкладку, скачать CSV
  и переименовать в канонiчное имя.
  - Все клики/проверки идут через `page.evaluate` с CSS-селекторами и строгой
    проверкой «найден ровно один элемент» — если 0 или >1, кидается
    `InterfaceChangedError`. Это защита от того, что Wordstat незаметно
    поменял разметку.
  - Скачивание файла детектируется через diff снапшота файлов в
    `run_directory` до/после клика (`_download_current_view`), с поллингом
    каждые 0.25s до `timeout_seconds`; если ничего не появилось —
    `DownloadTimeoutError`.
  - MVP жёстко ограничен четырьмя представлениями (`top_popular`,
    `top_related`, `dynamics`, `regions`); вкладка «Сайты по запросу»
    сознательно не реализована.

- **`storage.py`** — файловая изоляция запусков: каждый вызов `collect`
  создаёт новый неразрушающий каталог
  `output_root/runs/<UTC-таймстамп>-<slug(phrase)>[-N]` (суффикс `-N`
  добавляется при коллизии, каталог никогда не перезаписывается).
  `preserve_export` копирует скачанный файл под каноническое имя
  (`<view>.csv`), оставляя исходный файл загрузки нетронутым.
  `write_manifest` пишет `manifest.json` в UTF-8 без экранирования кириллицы.

- **`csv_io.py`** — парсинг только что скачанных CSV. Кодировка
  автоопределяется перебором (`utf-8-sig`, `utf-8`, `cp1251` — Wordstat
  экспортирует в cp1251), диалект — через `csv.Sniffer` с фолбэком на
  `excel`. Заголовки валидируются (непустые, уникальные), но их конкретные
  названия/язык — нет: они локализованы Wordstat и сохраняются как есть.
  Пустые строки (все значения — пробелы) отбрасываются.

- **`models.py`** — Pydantic-модели как единственный контракт форматов
  данных между слоями: `WordstatView` (enum четырёх отчётов), `CsvDataset`
  (распарсенный CSV), `ExportSummary`/`CollectionManifest` (метаданные для
  `manifest.json`), `CollectionResult` (что возвращает `collect`).

- **`errors.py`** — плоская иерархия от `WordstatError`; каждый тип ошибки
  соответствует конкретной причине сбоя (нет авторизации, изменился UI,
  не скачалось вовремя, не распарсился CSV). `cli.py` ловит
  `WordstatError | ValueError` и оборачивает в `click.ClickException`.

## Testing notes

Тесты (`tests/test_storage.py`, `tests/test_csv_io.py`) покрывают только
чистые файловые/парсинговые слои (`storage.py`, `csv_io.py`) — без сети и
без браузера. `collector.py` взаимодействует с реальным Chrome через CDP и
юнит-тестами не покрыт; проверять его поведение можно только вручную с
запущенным авторизованным Chrome.
