# wordstat

`wordstat` выгружает CSV из интерфейса Яндекс Вордстат через уже открытый и
авторизованный Chrome. Исходный `browser-use` подключается как внешняя локальная
editable-зависимость из соседнего каталога `../browser-use`.

## Установка

```bash
cd /Users/axisrow/Projects/wordstat
uv venv --python 3.11
source .venv/bin/activate
uv sync --all-groups
```

Chrome должен быть открыт с доступным CDP endpoint. По умолчанию используется
`http://127.0.0.1:9222`; другой endpoint передаётся через `--cdp-url` или
переменную окружения `WORDSTAT_CDP_URL`.

## Сбор

```bash
wordstat collect "ремонт квартир" --region "Москва" --output-dir ./wordstat-output
```

Каждый запуск создаёт новый каталог в `wordstat-output/runs/`. В нём остаются
исходные выгрузки Wordstat, канонические файлы `top_popular.csv`,
`top_related.csv`, `dynamics.csv`, `regions.csv` и `manifest.json`.

Команда не вводит учётные данные и прекращает работу, если Wordstat показывает
страницу входа. Она собирает только четыре согласованных с MVP представления:
популярные и похожие запросы, динамику и регионы. Вкладка «Сайты по запросу» в
первый релиз не входит.
