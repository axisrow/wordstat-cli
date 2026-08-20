# Референс-проекты: аналоги на GitHub

Карта публичных инструментов для Яндекс Вордстат и позиционирование `wordstat`
среди них. Документ отвечает на два вопроса: что уже написано другими и чем
этот проект отличается.

Срез ландшафта: **2026-08-20**. Как обновить — см. [«Как обновлять»](#как-обновлять).

Область — строго Вордстат. Google Keyword Planner, Google Trends и общий
SEO-тулинг сюда не входят.

## Как классифицируются аналоги

Главный разделитель — **способ доступа к данным**, а не набор фич. От него
зависит всё остальное: нужен ли платёжный аккаунт, упираешься ли ты в квоты или
в антибот, какие отчёты вообще доступны.

1. **Официальный API** — Yandex Cloud Search API v2 (реже устаревший Direct
   API). Нужен сервисный аккаунт, API-ключ и активный биллинг; взамен — стабильный
   контракт и явные квоты. Сюда попадает подавляющее большинство проектов, включая
   все самые звёздные. Старый OAuth Wordstat API выведен из эксплуатации (прямо
   отмечено в README [SvechaPVL/yandex-mcp](https://github.com/SvechaPVL/yandex-mcp)),
   поэтому свежие проекты сидят на Search API v2, а репозитории на старом API
   фактически мертвы — например
   [ivansky/php-yandex-wordstat](https://github.com/ivansky/php-yandex-wordstat)
   (push 2014) и [ne-coding](https://github.com/ne-coding/Yandex.Wordstat-parser)
   (push 2022).
2. **Headless-браузер** — Selenium / PhantomJS / Playwright, который инструмент
   запускает сам. Ключ и биллинг не нужны, но появляются логин, капча и прокси.
3. **Сторонний SERP-прокси** — XMLRiver и подобные. Платно, зато без своей
   инфраструктуры.
4. **Userscript / bookmarklet** — работают прямо в открытой странице Вордстата,
   дополняют интерфейс, а не экспортируют датасеты.

`wordstat` стоит в отдельной точке: **CDP-подключение к уже запущенному и
авторизованному пользовательскому Chrome**. Нет API-ключа, нет биллинга, нет
собственного логина и капчи — браузер уже авторизован пользователем, инструмент
лишь управляет им. Единственный найденный аналог по этому признаку —
`sophiaelowyn/yandex-wordstat-parser`.

## Ближайшие аналоги: браузерные экспортёры

| Проект | Язык | ★ | Доступ | Что нужно | Отчёты | Выход |
|---|---|---|---|---|---|---|
| **[axisrow/wordstat-cli](https://github.com/axisrow/wordstat-cli)** (этот) | Python | — | CDP к уже открытому Chrome | авторизованный Chrome | 4 вида: популярные, похожие, динамика, регионы | типизированный Parquet + `manifest.json` |
| [sophiaelowyn/yandex-wordstat-parser](https://github.com/sophiaelowyn/yandex-wordstat-parser) | Python | 3 | CDP `:9222` | Chrome + логин | популярные, похожие, динамика | JSON |
| [DiFlector/Wordstat-parser](https://github.com/DiFlector/Wordstat-parser) | Python | 7 | Selenium | chromedriver/geckodriver | частотность в 3 формах: базовая, точная, уточнённая | XLSX |
| [Fauros/yandex-wordstat-parser](https://github.com/Fauros/yandex-wordstat-parser) | Jupyter | 0 | свой браузер | интерактивный логин по ходу работы | динамика за 24 мес. | CSV на каждый ключ |
| [TechAlchemistry/yandex-wordstat-parser](https://github.com/TechAlchemistry/yandex-wordstat-parser) | PHP | 13 | PhantomJS | логин/пароль Яндекса, прокси, решатель капчи | левая/правая колонка (есть фильтр по регионам) | массив PHP |
| [z0mb1/wordstat_parser](https://github.com/z0mb1/wordstat_parser) | Python | 3 | парсинг страниц UI | — | частотность, 10 страниц выдачи | — (архивный, push 2019) |

Колонка «Отчёты» сверена с четырьмя представлениями `WordstatView`
(`src/wordstat/models.py`). Она различает проекты сильнее всего: большинство
парсеров ограничивается частотностью и похожими запросами.

Про регионы стоит различать две разные вещи — **фильтр по региону** (вход) и
**отчёт «Регионы»** (выход). По исходникам: `TechAlchemistry` принимает список
регионов как параметр запроса (`Query::getRegions()`), `DiFlector` жёстко
передаёт `region: 'all'`, `sophiaelowyn` и `Fauros` региона не касаются вовсе.
Собственно **отчёт с распределением спроса по регионам не выгружает ни один из
браузерных аналогов** — только `wordstat` и часть API-клиентов.

### Инженерные характеристики

Только проверяемые факты (`gh api repos/…` и обход дерева репозитория):

| Проект | Лицензия | Тесты | Последний push |
|---|---|---|---|
| axisrow/wordstat-cli | MIT | да (`tests/`, 5 файлов) | 2026-08 |
| sophiaelowyn/yandex-wordstat-parser | MIT | нет | 2026-08 |
| DiFlector/Wordstat-parser | нет | нет | 2025-09 |
| Fauros/yandex-wordstat-parser | нет | нет | 2026-06 |
| TechAlchemistry/yandex-wordstat-parser | MIT | нет | 2018-07 |
| z0mb1/wordstat_parser | нет | нет | 2019-04 |

Колонок «детект изменения вёрстки» и «resume/state» здесь намеренно нет:
заполнить их честно можно только вычитав исходники всех четырёх проектов, а
таблица с додуманными ячейками хуже узкой.

Со стороны `wordstat` соответствующие механизмы такие: `storage.py` создаёт на
каждый прогон отдельный неразрушающий каталог; `collector.py` проверяет, что
каждый CSS-селектор нашёл ровно один элемент, и кидает `InterfaceChangedError`,
если Вордстат поменял разметку; `dtypes.py` выводит типы по значениям, а не по
именам заголовков, и записывает результат в манифест. Как устроены те же аспекты
у аналогов — не проверялось.

### Про ближайший аналог

`sophiaelowyn/yandex-wordstat-parser` — единственный настоящий CDP-аналог. Это
один файл `wordstat_parser.py`: сырой CDP-WebSocket поверх `websockets`,
`Runtime.evaluate` для извлечения таблиц, результат в `ws_<ключ>.json` с полями
`popular` / `similar` / `dynamics`. В репозитории нет выбора региона, тестов и
изоляции запусков; зато есть batch по нескольким ключам сразу и связка с Google
Trends, чего нет здесь.

## API-клиенты, MCP-серверы и агентские скиллы

Другая категория: браузер не нужен вовсе, но нужен аккаунт Yandex Cloud с
API-ключом и активным биллингом. Взамен — квоты вместо риска блокировки и
стабильный контракт вместо парсинга вёрстки.

| Проект | Язык | ★ | Транспорт | Примечание |
|---|---|---|---|---|
| [artwist-polyakov/polyakov-claude-skills](https://github.com/artwist-polyakov/polyakov-claude-skills) | Shell | 183 | Search API v2 | набор скиллов для Claude Code, Вордстат — один из плагинов |
| [altrr2/yandex-tools-mcp](https://github.com/altrr2/yandex-tools-mcp) | JS | 60 | Search API v2, MCP | монорепо; npm-пакет `yandex-wordstat-mcp` |
| [SvechaPVL/yandex-mcp](https://github.com/SvechaPVL/yandex-mcp) | Python | 59 | Search API v2, MCP | 132 инструмента всего, из них 5 по Вордстату |
| [axelfreeman/yandex-wordstat-guide](https://github.com/axelfreeman/yandex-wordstat-guide) | Python | 30 | Search API v2 | скилл для Hermes Agent + гайд по OAuth |
| [Yurich-ru/yandex-ads-mcp](https://github.com/Yurich-ru/yandex-ads-mcp) | Python | 21 | Direct + Metrika + Wordstat, MCP | ~150 инструментов, упор на управление кампаниями |
| [nebelov/yandex-direct-for-all](https://github.com/nebelov/yandex-direct-for-all) | Python | 20 | Direct / Wordstat / Metrika | бандл плагинов для Codex |
| [stufently/yandex-mcp](https://github.com/stufently/yandex-mcp) | JS | 16 | Search / Wordstat / Webmaster / Metrika, MCP | набор отдельных MCP-серверов |
| [tigusigalpa/yandex-search-php](https://github.com/tigusigalpa/yandex-search-php) | PHP | 16 | Search API | SDK для Laravel 8–12, DTO и фасады |
| [Horosheff/yadryshko-semantic-core-subagent](https://github.com/Horosheff/yadryshko-semantic-core-subagent) | Python | 14 | Search API | субагент Cursor: семантика, кластеризация, отчёты |
| [ne-coding/Yandex.Wordstat-parser](https://github.com/ne-coding/Yandex.Wordstat-parser) | Python | 13 | Direct API | архивный: левая/правая колонка, push 2022 |
| [mkultraaaa/claude-yandex-skills](https://github.com/mkultraaaa/claude-yandex-skills) | Shell | 12 | Search / Direct / Metrika / Webmaster | скиллы для Claude Code, cache-first |
| [georgy-agaev/yandex-direct-metrica-mcp](https://github.com/georgy-agaev/yandex-direct-metrica-mcp) | Python | 8 | Direct + Metrika + Wordstat, MCP | read-only + pro, дашборды и BI-синк |
| [ivansky/php-yandex-wordstat](https://github.com/ivansky/php-yandex-wordstat) | PHP | 8 | Direct API (OAuth) | мёртвый пример: push 2014, API выведен из эксплуатации |
| [antohins/seo-tools-mcp](https://github.com/antohins/seo-tools-mcp) | TS | 3 | Wordstat / SERP / GSC / Metrika, MCP | 8 stdio-серверов, read-only, мультиаккаунт |
| [askads/mcp-yandex-wordstat](https://github.com/askads/mcp-yandex-wordstat) | TS | 4 | Search API v2, MCP | 5 инструментов, покрывает все 4 вида отчётов + справочник регионов |
| [Devvver/Wordstat_yandex_api](https://github.com/Devvver/Wordstat_yandex_api) | Python | 3 | Search API v2 | Streamlit-UI, рекурсивный обход до N уровней, экспорт CSV/XLSX |
| [Vlad-Loop/n8n-nodes-wordstat](https://github.com/Vlad-Loop/n8n-nodes-wordstat) | TS | 3 | Search API, n8n | нода для workflow-автоматизации |
| [IgorShkarin/yandex-wordstat-collector](https://github.com/IgorShkarin/yandex-wordstat-collector) | JS | 2 | Search API v2 | **лучший пример устойчивости**: resume, quota ledger, atomic checkpoint |
| [M1shut3r/Parser_Wordstat_Yandex](https://github.com/M1shut3r/Parser_Wordstat_Yandex) | Python | 1 | Search API v2 | десктопный GUI (CustomTkinter), мультиаккаунт, обработка 429 |
| [artgas1/xmlriver-mcp](https://github.com/artgas1/xmlriver-mcp) | Python | 1 | XMLRiver, MCP | сторонний SERP-прокси, не Яндекс напрямую |

## Userscript и bookmarklet

Работают в уже открытой странице Вордстата, дополняют интерфейс, а не
экспортируют датасеты:

- [sc00d/yandex-wordstat-labels](https://github.com/sc00d/yandex-wordstat-labels) —
  подписывает точки на графике истории и добавляет колонку «Динамика к АППГ».
- [antoniolite/bookmarklet-yandex-wordstat](https://github.com/antoniolite/bookmarklet-yandex-wordstat) —
  букмарклет, вытаскивающий ключи и показы с текущей страницы.

## Совпадения по имени — не аналоги

Поиск по слову `wordstat` выдаёт много счётчиков частотности слов в тексте, к
Яндексу отношения не имеющих. Чтобы их не пересматривать заново:
`pyaggi/WordStats`, `netcan/wordStatistics`, `Systemcluster/wordstat`,
`maxnd/wordstatix`, `GALA-X-Y/WordStats`, `jfhovinne/wordStats`,
`zeeguu/python-wordstats`, `sevenmaxis/wordstats`, `prxkhxr-02/WordStat`,
`shnoor64/wordStatFromURL`, `davidtchiu/cs475-hwk1-wordstat`,
`wsl1999/Annual_Report_Wordstats` (китайские годовые отчёты).

## Выводы для проекта

- **Ниша почти свободна.** Подход «CDP к живому авторизованному Chrome» занят
  ровно одним однофайловым скриптом на 3 звезды. Остальной ландшафт требует
  Yandex Cloud с биллингом либо поднимает собственный браузер с логином и капчей.
- **Типизированный Parquet + манифест не делает никто.** Аналоги отдают JSON,
  CSV или XLSX без схемы; проверить, что Вордстат поменял формат выгрузки, по ним
  нельзя.
- **Отчёт «Регионы» — редкое покрытие.** Среди браузерных аналогов его не
  выгружает никто (у части есть только фильтр по региону на входе); среди
  API-клиентов он доступен там, где реализованы все методы Search API v2 —
  например у [askads/mcp-yandex-wordstat](https://github.com/askads/mcp-yandex-wordstat).
- **Чего здесь нет и на что стоит посмотреть:**
  - resume / retry / учёт квот — образец в
    [IgorShkarin/yandex-wordstat-collector](https://github.com/IgorShkarin/yandex-wordstat-collector);
  - batch по нескольким фразам за прогон (сейчас одна фраза на запуск) — есть у
    `sophiaelowyn` и `DiFlector`;
  - MCP-обёртка как способ доставки — самый популярный формат в этой нише
    (`askads`, `altrr2`, `SvechaPVL`).

## Как обновлять

```bash
gh api -X GET search/repositories -f q='wordstat' -f sort=stars -f per_page=40 \
  --jq '.items[] | [.full_name, (.stargazers_count|tostring), (.language // "-"), (.pushed_at[0:10]), (.description // "-")] | join(" | ")'
```

Полезно повторить с `q='вордстат'` и `q='wordstat in:readme'` — часть проектов
не содержит слова `wordstat` в имени или описании. Обновив список, поправьте дату
среза в начале файла.
