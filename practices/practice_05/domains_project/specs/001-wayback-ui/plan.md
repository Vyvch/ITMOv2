# Implementation Plan: История домена в веб-архиве (интерфейс)

**Branch**: `001-wayback-ui` (работа ведётся в `practice_05`) | **Date**: 2026-10-28 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/001-wayback-ui/spec.md`; общий контракт
`specs/wayback-contract.md` v1.2 (приоритетен при расхождениях). Серверная сторона —
`specs/002-wayback-api` (другая команда), здесь не проектируется.

## Summary

Во вкладке «Все домены» карточки сетки становятся кнопками и открывают диалог домена со ссылкой
«Открыть в веб-архиве» (punycode-хост, без запросов) и кнопкой «Запросить историю». Во вкладке
Топ-20 добавляется колонка «Веб-архив»: ссылка сразу и кнопка «Запросить историю» в каждой строке —
автозагрузка отклонена по замерам сервера (холодный запрос ~17 с, архив по одному, research R9).
Логика без DOM — разбор ответов `/api/wayback`, повтор `202` через `retry_after`, дедлайн 2 минуты,
таймаут запроса 20 с, отсчёт `Retry-After`, отмена устаревших ответов, запоминание результатов,
ограничение 4 параллельных запросов (слот только на время запроса) — собирается в блоке `<script id="wayback-core">` внутри
`templates/index.html` и тестируется из `unittest` через `node` (без npm). Строки Топ-20 и сетка
переводятся на `createElement`/`textContent` (принцип V). Подробности — [research.md](research.md).

## Technical Context

**Language/Version**: JavaScript (ES2022, встроенный в `templates/index.html`); тесты — Python 3.14.1;
Node.js 24 (установлен) как движок для тестов логики.

**Primary Dependencies**: нет новых. Браузерные API: `fetch`, `AbortController`, `<dialog>`, `URL`,
`Intl.NumberFormat`. В тестах — встроенный модуль Node `vm`.

**Storage**: N/A — состояние только в памяти страницы (`Map` до перезагрузки).

**Testing**: `unittest` (`tests/test_wayback_ui.py`): статические проверки текста `index.html`
и сценарии `WaybackCore` в `node` с подменёнными `fetch`/часами/таймерами; запуск —
`python scripts/check.py`.

**Target Platform**: современные браузеры с `<dialog>` (Chrome/Edge/Firefox/Safari 2022+);
сервер FastAPI проекта.

**Project Type**: web-service (FastAPI-мини-сайт), фронтенд — один HTML-файл.

**Performance Goals**: индикатор загрузки < 0,1 с после нажатия (SC-003); итог каждого блока
≤ 2 мин после нажатия (SC-003, SC-004); 0 запросов истории при открытии Топ-20 (SC-004);
повторное открытие карточки — мгновенно из памяти.

**Constraints**: только `/api/wayback` через сервер (принцип IV); ≤ 4 одновременных запросов
истории; таймаут запроса 20 с; дедлайн 120 с; внешние данные — только как текст (принцип V);
без сборщиков и npm (принцип I).

**Scale/Scope**: 1 файл UI (~+300 строк JS/CSS), 1 новый тестовый модуль; 2 экрана (диалог,
колонка Топ-20); 8 видимых состояний блока истории.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Принцип | Проверка | До Phase 0 | После Phase 1 |
|---------|----------|------------|---------------|
| I. Простой стек | Всё в `templates/index.html`, инлайн JS/CSS; нет npm, сборщиков, фреймворков; новых Python-зависимостей нет. Node — только движок для тестов (см. Complexity Tracking) | PASS | PASS |
| II. Сначала тесты | Каждая задача начинается с падающего теста в `tests/test_wayback_ui.py` на `unittest`; сеть не используется — `fetch` подменяется; готовность — `python scripts/check.py` = 0 | PASS | PASS |
| III. Контракт и runner | `docs/requirements.md` и `scripts/check.py` не меняются; `specs/wayback-contract.md` не меняется — интерфейс только следует ему | PASS | PASS |
| IV. Внешние сервисы через сервер | Клиент обращается только к `/api/wayback`; ссылки на `web.archive.org` — навигация пользователя, не запросы к API (спецификация, допущения); статический тест «`fetch` только к `/api/`» | PASS | PASS |
| V. Безопасный вывод в DOM | Новый код — только `createElement`/`textContent`/`href`; существующие нарушения устраняются: HTML-вставки в сетке (`index.html:485`) и Топ-20 (`index.html:568`), некодированный параметр `fetch(\`/api/top?date_type=${dateType}\`)` (`index.html:542`, T004); статические тесты запрещают HTML-вставки, кроме `innerHTML = ''`, и требуют `encodeURIComponent` для каждого параметра `fetch` | PASS | PASS |
| Тех. ограничения: `.рф`, русский язык | `.рф` показывается кириллицей, в адрес архива — punycode; запрос к серверу — в форме из списка (контракт, правило 1); все артефакты на русском | PASS | PASS |
| Процесс: код-ревью | Перед завершением — скилл `code-review` с проверкой принципов IV и V | PASS | PASS |

Нарушений нет; Complexity Tracking фиксирует одно обоснованное решение.

## Project Structure

### Documentation (this feature)

```text
specs/wayback-contract.md        # общий контракт с серверной командой (источник истины)
specs/001-wayback-ui/
├── spec.md
├── plan.md              # этот файл
├── research.md          # Phase 0
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   └── ui-contract.md   # Phase 1: WaybackCore, якоря DOM, тексты, запреты
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks, не создаётся здесь)
```

### Source Code (repository root)

```text
templates/
└── index.html           # + <script id="wayback-core"> (логика без DOM)
                         # + <dialog id="domain-dialog">, колонка «Веб-архив» в #top-table
                         # renderDomains / loadTop → createElement + textContent
tests/
└── test_wayback_ui.py   # новый: статические проверки + сценарии WaybackCore в node
```

Сервер (`api.py`, `services.py`, новый `wayback.py`) не трогается — зона `specs/002-wayback-api`.

**Structure Decision**: плоская структура проекта сохраняется; весь UI — в одном
`templates/index.html` (принцип I). Граница «логика / DOM» проходит между блоком
`wayback-core` и основным скриптом страницы — это и есть точка тестирования.

## Порядок работ (вход для `/speckit-tasks`)

1. Статические тесты безопасности → перевод сетки «Все домены» и строк Топ-20 на `createElement`
   (US4, FR-020…FR-022) — независимо от сервера.
2. Тесты и реализация `archiveUrl`, `formatDate`, `formatCount`, `safeHref` → диалог домена
   со ссылкой (US1, P1).
3. Табличные тесты `classifyResponse` → `createLimiter`, `createMemo` → `createHistoryLoader`
   на виртуальных часах.
4. Блок истории в диалоге: состояния, «Повторить», отсчёт `Retry-After` (US2).
5. Колонка «Веб-архив» в Топ-20: ссылка и кнопка «Запросить историю» в каждой строке, без
   автозапуска; отмена загрузок при смене списка (US3).
6. Ручная проверка по [quickstart.md](quickstart.md), `code-review`.

## Риски

| Риск | Мера |
|------|------|
| Серверный эндпоинт ещё не готов | US1 и US4 от него не зависят; US2–US3 проверяются на подменённых ответах; формат зафиксирован контрактом |
| На машине без node тесты логики пропускаются | статические тесты работают всегда; T028 закрывается только при 0 пропущенных тестов |
| Изменение вида карточек сетки при замене `div` → `button` | CSS-сброс стилей кнопки; ручная сверка до/после (SC-007) |
| Холодный запрос к архиву ~17 с — история приходит после 1–2 повторов `202` | состояние «Загрузка истории…» держится до 2 мин; автозагрузка в Топ-20 отключена, чтобы не ставить 20 доменов в очередь сервера |

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Node.js как движок для тестов JS-логики (не сборщик, без npm) | Машина состояний с таймерами (202, 20 с, 2 мин, отсчёт, отмена) требует исполнения JS; принцип II требует тестов до кода | Только статические проверки не покрывают поведение; `py_mini_racer`/Playwright — новые тяжёлые зависимости |
