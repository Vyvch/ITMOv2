---

description: "Задачи реализации интерфейса «История домена в веб-архиве»"
---

# Tasks: История домена в веб-архиве (интерфейс)

**Input**: Design documents from `specs/001-wayback-ui/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ui-contract.md, quickstart.md;
общий контракт `specs/wayback-contract.md` v1.2 (приоритетен).

**Tests**: обязательны — принцип II конституции (TDD). Каждая тестовая задача выполняется ДО
соответствующей реализации, и тест MUST упасть перед реализацией. Все тесты — `unittest`
в `tests/test_wayback_ui.py`; проверка — `python scripts/check.py`. Сеть в тестах не используется.

**Organization**: задачи сгруппированы по историям. Почти весь код — в одном файле
`templates/index.html` (принцип I), поэтому меток [P] мало: параллельно можно вести только задачи
в разных файлах без зависимостей.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: можно выполнять параллельно (разные файлы, нет зависимостей)
- **[Story]**: история из spec.md (US1…US4)

## Path Conventions

- UI: `templates/index.html` — блок `<script id="wayback-core">` (логика без DOM, `globalThis.WaybackCore`)
  и основной `<script>` страницы (DOM).
- Тесты: `tests/test_wayback_ui.py`.
- Сервер (`api.py`, `services.py`, `wayback.py`) НЕ трогать — зона `specs/002-wayback-api`.
- Не менять `docs/requirements.md`, `scripts/check.py`, `specs/wayback-contract.md`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: тестовая обвязка для запуска `WaybackCore` в node и пустой блок ядра.

- [ ] T001 Создать `tests/test_wayback_ui.py` с хелперами: `read_index_html()` читает `templates/index.html`
  (UTF-8); `load_core_source()` извлекает текст блока `<script id="wayback-core">` через `html.parser`;
  `run_core(scenario_js)` запускает `node` (путь из `shutil.which("node")`) с `vm.runInNewContext`:
  в песочницу кладутся исходник ядра, затем `scenario_js`; возвращает разобранный JSON из stdout.
  Песочница MUST явно получать веб-API, которых нет в новом контексте `vm`: `URL`, `AbortController`,
  `console` (встроенные `JSON`, `Promise`, `Intl`, `encodeURIComponent` в контексте уже есть).
  Сценарии асинхронные: обвязка оборачивает `scenario_js` в `async`-функцию, `await` её результат
  и печатает `JSON.stringify(result)`; исключение в сценарии → ненулевой код выхода и текст ошибки
  в сообщении теста. Базовый класс `CoreTestCase.setUp` вызывает `self.skipTest("node не найден
  в PATH: тесты WaybackCore пропущены")`, если node нет. Все хелперы — с аннотациями типов
  (`def run_core(scenario_js: str) -> Any`, принцип I). Первый тест `test_core_exposes_constants`:
  в `WaybackCore` есть `DEADLINE_MS === 120000`, `REQUEST_TIMEOUT_MS === 20000`, `MAX_PARALLEL === 4`
  (тест падает).
- [ ] T002 Добавить в `templates/index.html` перед основным `<script>` блок `<script id="wayback-core">`
  с IIFE, публикующей `globalThis.WaybackCore = { DEADLINE_MS: 120000, REQUEST_TIMEOUT_MS: 20000,
  MAX_PARALLEL: 4 }`. Блок не обращается к `document`, `window`, глобальным `fetch`/`setTimeout`.
  T001 проходит.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: общий DOM-хелпер и страховочный тест границы «клиент ходит только в `/api/`».

**⚠️ CRITICAL**: истории начинаются после этой фазы.

- [ ] T003 В `tests/test_wayback_ui.py` добавить класс `StaticChecks` (выполняется всегда, без node):
  `test_fetch_only_relative_api` — каждый вызов `fetch(` в `templates/index.html` начинается
  с литерала `'/api/`, `"/api/` или `` `/api/ `` (принцип IV); `test_api_params_encoded` — в каждом
  вызове `fetch(` все подстановки `${…}` в шаблонной строке имеют вид `${encodeURIComponent(…)}`,
  а все конкатенации `+ …` — вид `+ encodeURIComponent(…)` (принцип V; падает на
  `fetch(\`/api/top?date_type=${dateType}\`)` в `loadTop`); `test_core_block_present` — блок
  `<script id="wayback-core">` существует ровно один.
- [ ] T004 В `loadTop()` `templates/index.html` заменить запрос на
  `fetch('/api/top?date_type=' + encodeURIComponent(dateType))` (T003 `test_api_params_encoded`
  проходит). В основном `<script>` добавить хелпер `el(tag, props = {}, children = [])`:
  `props.className`, `props.text` → `textContent`, `props.href` → свойство `href`, `props.attrs` →
  `setAttribute` для каждой пары, `props.style` → `Object.assign(node.style, …)`, `props.on` → `addEventListener`;
  `children` — узлы или строки (строки → `document.createTextNode`). `innerHTML` не используется.

**Checkpoint**: `python scripts/check.py` зелёный; можно начинать истории.

---

## Phase 3: User Story 1 — Быстрый переход в веб-архив из карточки домена (Priority: P1) 🎯 MVP

**Goal**: клик по домену во «Всех доменах» открывает карточку со ссылкой «Открыть в веб-архиве»,
без запросов к серверу.

**Independent Test**: кликнуть по домену → открыт `dialog#domain-dialog` с именем, датой, ссылкой
`https://web.archive.org/web/*/<домен>` (punycode для `.рф`) и кнопкой «Запросить историю»;
запросов `/api/wayback` нет; Esc/«Закрыть»/клик по фону закрывают, фильтры и страница прежние.

### Tests for User Story 1 ⚠️

- [ ] T005 [US1] В `tests/test_wayback_ui.py` (класс на `CoreTestCase`) — тесты `archiveUrl`:
  `'пример.рф'` → `'https://web.archive.org/web/*/xn--e1afmkfd.xn--p1ai'`; `'Example.RU'` →
  `'https://web.archive.org/web/*/example.ru'`; `'alpha.su'` → `'https://web.archive.org/web/*/alpha.su'`.
- [ ] T006 [US1] В `StaticChecks` (`tests/test_wayback_ui.py`) — тест якорей диалога по
  `contracts/ui-contract.md` §2: есть `<dialog id="domain-dialog" aria-labelledby="domain-dialog-title">`,
  `id="domain-dialog-title"`, `id="domain-dialog-date"`, элемент с классом `wayback-link` и атрибутами
  `target="_blank"` и `rel="noopener noreferrer"`, кнопки с классами `domain-dialog-close`
  и `wayback-request`; функция `renderDomains` создаёт `'button'` с классом `domain-card`
  (через `el(`), а не `div`.

### Implementation for User Story 1

- [ ] T007 [US1] Реализовать `WaybackCore.archiveUrl(domain)` в блоке `wayback-core` `templates/index.html`:
  `'https://web.archive.org/web/*/' + new URL('http://' + domain.trim()).hostname` (punycode и нижний
  регистр делает `URL`). T005 проходит.
- [ ] T008 [US1] Добавить в `templates/index.html` разметку `<dialog id="domain-dialog"
  aria-labelledby="domain-dialog-title">`: заголовок `#domain-dialog-title`, `#domain-dialog-date`
  («Дата освобождения: …»), `<a class="wayback-link" target="_blank" rel="noopener noreferrer">Открыть
  в веб-архиве</a>`, контейнер `<div class="wayback-history" role="status" aria-live="polite">` с кнопкой
  `<button type="button" class="wayback-request">Запросить историю</button>`, кнопка
  `<button type="button" class="domain-dialog-close">Закрыть</button>`. CSS в существующем `<style>`:
  диалог в стиле карточек (`var(--border)`, `var(--fg)`), `::backdrop` полупрозрачный, длинные имена
  переносятся (`word-break: break-all`). T006 (часть диалога) проходит.
- [ ] T009 [US1] Переписать `renderDomains()` в `templates/index.html`: карточка —
  `el('button', {className: 'domain-card', attrs: {type: 'button'}}, [el('div', {className: 'd-name',
  text: d.domain}), el('div', {className: 'd-date', text: d.date})])` с обработчиком клика
  `openDomainDialog(d)`; убрать `div.innerHTML = …` (FR-022). В CSS `.domain-card` сбросить стили кнопки
  (`font: inherit; color: inherit; background: …; text-align: left; width: 100%; cursor: pointer`), вид
  сетки не меняется; видимый `:focus-visible`. T006 проходит полностью.
- [ ] T010 [US1] Реализовать в `templates/index.html` `openDomainDialog(d)`: `textContent` для заголовка
  и даты; `href` ссылки = `WaybackCore.archiveUrl(d.domain)`; блок истории в исходном состоянии
  (только «Запросить историю», без запроса — FR-006); `dialog.showModal()`. Закрытие: кнопка «Закрыть»
  и клик по фону (`event.target === dialog`) → `dialog.close()`, Esc закрывает нативно. Вся уборка
  при закрытии — в одном обработчике события `close` элемента `dialog` (срабатывает при всех трёх
  способах). Фильтры, поиск, regex и `currentPage` не трогаются (FR-004). Ручная проверка: шаг 1
  `quickstart.md`.

**Checkpoint**: US1 работает без серверной фичи; `python scripts/check.py` зелёный.

---

## Phase 4: User Story 2 — Запрос истории снимков в карточке домена (Priority: P2)

**Goal**: «Запросить историю» → «Загрузка истории…» → итог: число снимков с датами и ссылками,
«Нет снимков», одна из ошибок с «Повторить» (с отсчётом при перегрузке; без кнопки при `invalid_domain`).

**Independent Test**: на подменённых ответах `/api/wayback` (200, 0 снимков, 202, 503, 504, 502, 400)
блок проходит «Загрузка» → нужное итоговое состояние; таймеры — виртуальные (тесты ядра в node).

### Tests for User Story 2 ⚠️

- [ ] T011 [US2] В `tests/test_wayback_ui.py` — тесты форматирования (FR-009, FR-009a, FR-010):
  `formatDate('2009-03-15')` → `'15.03.2009'`; `formatDate('2009-3-15')`, `formatDate('')`,
  `formatDate(null)` → `'—'`; `formatCount(1234, false)` → `'1 234'`; `formatCount(100000, true)` →
  `'100 000+'`; `formatCount(0, false)` → `'0'`; `safeHref('https://web.archive.org/web/1/x')` →
  та же строка; `safeHref('http://web.archive.org/web/1/x')`, `safeHref('https://a.ru/')`,
  `safeHref('https://web.archive.org.evil.ru/')`, `safeHref('javascript:alert(1)')`, `safeHref('')`,
  `safeHref(null)`, `safeHref('not a url')` → `null`.
- [ ] T012 [US2] В `tests/test_wayback_ui.py` — табличный тест `classifyResponse(status, retryAfterHeader,
  bodyText)` по data-model.md «WaybackResult»: 200 + `snapshot_count: 1234` с `first`/`last` → `kind:
  'success'`, `count: 1234`, `truncated: false`, `first: {dateText: '15.03.2009', href: …}`; 200 +
  `count_truncated: true` → `truncated: true`; 200 + `snapshot_count: 0` → `empty`; 200 без числового
  `snapshot_count` или с отрицательным/дробным → `failed`; 200 + `first: null` при `count > 0` → `first:
  null`; 200 + `first.url: 'javascript:…'` или `'https://a.ru/'` → `first.href: null`; 202 + `retry_after: 8` → `pending`,
  `retryAfterSec: 8`; 202 без поля + заголовок `'5'` → `5`; 202 без поля и заголовка → `1`;
  503 + `archive_overloaded` + заголовок `'12'` → `overloaded`, `retryAfterSec: 12`; заголовок
  отсутствует / `'abc'` / `'0'` → `retryAfterSec: null`; 504 + `archive_timeout` → `timeout`,
  `reason: 'server'`; 502 + `archive_error` → `failed`; 400 + `invalid_domain` → `invalid`;
  неизвестный `code`, 503 с чужим `code`, статус 500/418, тело не JSON → `failed`. Поле `detail`
  не влияет на результат (FR-012).
- [ ] T013 [US2] В `tests/test_wayback_ui.py` — тесты `createMemo()`, `createLimiter(max)` и
  `createCountdown(seconds, deps, onTick)`: memo хранит только `success`/`empty`, ключ
  `domain.toLowerCase()` (`put('Example.RU')` → `get('example.ru')`), `pending`/ошибки не сохраняются;
  `createLimiter(2)`: третья задача стартует только после завершения одной из двух; задача в очереди,
  чей `signal` прерван, удаляется и не запускается; порядок FIFO; `createCountdown(3, …)` на виртуальных
  часах: `onTick` получает `3` сразу, затем `2`, `1`, `0` на 1000, 2000, 3000 мс, больше вызовов нет;
  `cancel()` на 1500 мс — после него `onTick` не вызывается (FR-012b).
- [ ] T014 [US2] В `tests/test_wayback_ui.py` — сценарии `createHistoryLoader(domain, deps)` на виртуальных
  часах (фейковые `now`/`setTimer`/`clearTimer` с ручным продвижением времени, фейковый `fetchFn`
  с управляемыми ответами), по data-model.md «Переходы»:
  (a) запись в memo → сразу `onState` с ней, `fetchFn` не вызван;
  (b) URL запроса — `'/api/wayback?domain=' + encodeURIComponent('пример.рф')`;
  (c) 200 → `onState` `{kind:'loading'}`, затем `success`; результат записан в memo;
  (d) 202 `retry_after: 8` → второй вызов `fetchFn` не раньше 8000 мс после ответа, затем 200 → `success`;
  (e) 202 постоянно с `retry_after: 30` → `timeout` с `reason: 'deadline'` ровно на 120 000 мс от `start()`,
  повтор, приходящийся после дедлайна, не отправляется;
  (f) `fetchFn` не отвечает → `timeout` с `reason: 'request'` через 20 000 мс от фактической отправки,
  `signal` прерван;
  (g) `fetchFn` отклоняет промис (сетевая ошибка) → `failed`;
  (h) `cancel()` во время запроса → `signal` прерван, после этого `onState` не вызывается даже при
  позднем ответе, таймеры сняты;
  (i) ответ, пришедший после дедлайна, игнорируется;
  (j) `start()` после итоговой ошибки → новая попытка с новым дедлайном 120 000 мс;
  (k) с `limiter = createLimiter(1)` вторая попытка ждёт слот, её 20-секундный таймаут отсчитывается
  от фактической отправки, а дедлайн — от `start()`;
  (l) с `limiter = createLimiter(1)`: первый загрузчик получил `202` с `retry_after: 8` → пока он ждёт
  повтора, второй загрузчик сразу получает слот и отправляет запрос; повтор первого занимает слот
  заново после освобождения.
- [ ] T015 [US2] В `StaticChecks` (`tests/test_wayback_ui.py`) — тест текстов интерфейса из
  `contracts/ui-contract.md` §3: в `templates/index.html` есть строки «Загрузка истории…», «Снимков: »,
  «Первый: », «Последний: », «Нет снимков в веб-архиве», «Веб-архив перегружен, попробуйте позже»,
  «Веб-архив не ответил вовремя», «Не удалось получить историю», «Повторить», «Повторить через »;
  есть класс `wayback-retry`.

### Implementation for User Story 2

- [ ] T016 [US2] Реализовать в блоке `wayback-core` `templates/index.html` `formatDate` (регулярное
  выражение `^(\d{4})-(\d{2})-(\d{2})$`, перестановка частей, без `Date`), `formatCount`
  (`Intl.NumberFormat('ru-RU').format(count)`, при `truncated` + `'+'`), `safeHref`
  (`new URL(url)` в `try`, только `protocol === 'https:'` и `hostname === 'web.archive.org'`).
  T011 проходит.
- [ ] T017 [US2] Реализовать `WaybackCore.classifyResponse` в `templates/index.html` строго по таблице
  data-model.md: «`count` — целое ≥ 0; иначе ответ → `failed`», «`truncated` — строго
  `count_truncated === true`», «`first`/`last` — объект с `date` и `url`, иначе `null`»,
  `pending.retryAfterSec` «≥ 1; тело `retry_after`, иначе заголовок, иначе 1», `overloaded.retryAfterSec`
  «заголовок `Retry-After`; некорректный → `null`». Ветвление по статусу, для ошибок — по `code`;
  `JSON.parse` в `try`. T012 проходит.
- [ ] T018 [US2] Реализовать `WaybackCore.createMemo()`, `WaybackCore.createLimiter(max)` и
  `WaybackCore.createCountdown(seconds, deps, onTick)` в `templates/index.html` (memo: `Map`, «ключ —
  `domain.toLowerCase()`, значения только `success`/`empty`»; limiter: очередь FIFO, `run(task, signal)`
  возвращает промис, отменённые ожидающие задачи удаляются; countdown: `onTick(seconds)` сразу, затем
  раз в 1000 мс через `deps.setTimer` до `onTick(0)`, `cancel()` снимает таймер). T013 проходит.
- [ ] T019 [US2] Реализовать `WaybackCore.createHistoryLoader(domain, deps)` в `templates/index.html`
  по research.md R4 и contracts/ui-contract.md §1: поколение, `AbortController` на запрос, дедлайн
  `DEADLINE_MS` от `start()`, таймаут `REQUEST_TIMEOUT_MS` от фактической отправки через `deps.limiter`,
  повтор `pending` не раньше `retryAfterSec` и только если успевает до дедлайна, запись итога в
  `deps.memo`, `cancel()` снимает все таймеры. Слот `deps.limiter` занимается только на один запрос
  и освобождается сразу после ответа или таймаута; ожидание повтора после `202` идёт вне слота.
  T014 проходит.
- [ ] T020 [US2] В основном `<script>` `templates/index.html` реализовать
  `renderHistory(container, result, handlers)` через `el()`: состояния и тексты строго по таблице
  data-model.md «HistoryBlockState»; `success` — «Снимков: N» (`formatCount`), «Первый: ДД.ММ.ГГГГ»
  и «Последний: ДД.ММ.ГГГГ» ссылками (`target="_blank" rel="noopener noreferrer"`) при `href`, иначе
  текстом, `—` при `null`; `error-overloaded` — `button.wayback-retry` `disabled`, отсчёт через
  `WaybackCore.createCountdown(retryAfterSec, deps, n => …)`: при `n > 0` текст «Повторить через n с»,
  при `0` — «Повторить» и кнопка активна (логика времени только в ядре); `retryAfterSec: null` —
  активна сразу; `error-invalid` («Ошибка: некорректный домен») — текст «Не удалось получить историю»
  без кнопки; остальные ошибки — «Повторить». Функция возвращает `dispose()`, который вызывает
  `cancel()` отсчёта. T015 проходит.
- [ ] T021 [US2] Связать диалог с ядром в `templates/index.html`: общие на страницу
  `const waybackLimiter = WaybackCore.createLimiter(WaybackCore.MAX_PARALLEL)` и
  `const waybackMemo = WaybackCore.createMemo()`; `deps` с `fetch`, `Date.now`, `setTimeout`,
  `clearTimeout`; «Запросить историю» и «Повторить» → `loader.start()`, кнопка недоступна на время
  загрузки (FR-008); при открытии диалога, если в memo есть результат, показать его без запроса
  (FR-015, сценарий 12 US2), иначе исходное состояние; `loader.cancel()` и `dispose()` отсчёта
  вызываются в обработчике события `close` диалога из T010 — значит, при закрытии кнопкой, Esc
  и кликом по фону (FR-014, пограничные случаи с закрытием). Ручная проверка: шаги 2 и 5
  `quickstart.md`.

**Checkpoint**: US1 и US2 работают; без серверной фичи US2 проверяется тестами ядра и DevTools Overrides.

---

## Phase 5: User Story 4 — Безопасный вывод карточек Топ-20 (Priority: P3)

**Goal**: строки Топ-20 строятся только через `createElement`/`textContent`, внешний вид прежний.
Идёт перед US3, потому что US3 добавляет колонку в эти же строки.

**Independent Test**: подменить `/api/top` обоснованием `<img src=x onerror=alert(1)>` и WHOIS с тегами →
всё видно буквально, скрипт не выполняется; обычные данные выглядят как раньше.

### Tests for User Story 4 ⚠️

- [ ] T022 [US4] В `StaticChecks` (`tests/test_wayback_ui.py`) — тест запретов `contracts/ui-contract.md` §4:
  в `templates/index.html` нет `outerHTML`, `insertAdjacentHTML`, `document.write`; каждое присваивание
  `innerHTML` имеет вид `innerHTML = ''` (регулярное выражение по всем вхождениям `innerHTML\s*=`).
  Тест падает на текущем `loadTop` (`tr.innerHTML = …`).

### Implementation for User Story 4

- [ ] T023 [US4] Переписать построение строки в `loadTop()` `templates/index.html` через `el()`:
  ячейка домена (`domain-name`, стили `width:25%; word-break: break-all`) — текст домена, `<br>`, `span`
  «Дроп: {date}» (`font-size:0.8rem; font-weight:normal; color:#666`); ячейка обоснования
  (`width:35%; font-size:0.9rem`) — `text: item.reason`; ячейка WHOIS (`width:40%`) — те же четыре ветки:
  «Свободен» → `span` зелёный жирный «✅ {status}»; `w.error` → `span` красный «❌ {error}»; поля →
  `strong` «Регистратор:» + текст, `br`, `strong` «Создан:» + текст, `br`, `strong` «Истекает:» + текст,
  `br`, `small` (`strong` «Статус:» + текст), `?` для пустых; иначе `pre` (`font-size:0.75rem;
  overflow-x:auto`) с `text: JSON.stringify(w, null, 2)`. Запрос `/api/top` остаётся с
  `encodeURIComponent(dateType)` из T004. T022 проходит. Ручная проверка: шаг 4
  `quickstart.md` (SC-006, SC-007).

**Checkpoint**: все HTML-вставки внешних данных устранены; статические тесты зелёные.

---

## Phase 6: User Story 3 — История по запросу во вкладке Топ-20 (Priority: P3)

**Goal**: колонка «Веб-архив» в каждой строке Топ-20: ссылка сразу и кнопка «Запросить историю»;
история грузится только по нажатию; строки независимы; смена списка прекращает загрузки прежнего.

**Independent Test**: открыть Топ-20 → запросов `/api/wayback` нет, в каждой строке ссылка и кнопка;
нажать кнопку в нескольких строках с подменёнными ответами (успех / нет снимков / ошибка) → каждая
строка независимо показывает своё состояние, остальные в исходном; одновременно ≤ 4 запросов;
«Повторить» перезапрашивает только свой домен; переключение «Сегодня/Завтра» прекращает загрузки
прежнего списка.

### Tests for User Story 3 ⚠️

- [ ] T024 [US3] В `StaticChecks` (`tests/test_wayback_ui.py`) — тесты: в `<thead>` таблицы `#top-table`
  есть `<th>Веб-архив</th>` четвёртой колонкой после «WHOIS Инфо»; функция `startTopHistory`
  существует, а тело функции `loadTop` не содержит `createHistoryLoader(` и `.start(` — загрузчики
  создаются только по нажатию (FR-018, `contracts/ui-contract.md` §4).
- [ ] T025 [US3] В `tests/test_wayback_ui.py` — сценарии ядра для Топ-20 на виртуальных часах:
  (a) 6 загрузчиков, запущенных подряд, с общим `createLimiter(4)` → в полёте не более 4 запросов,
  остальные стартуют по мере освобождения слотов; (b) ошибка одного домена не меняет состояния
  остальных (FR-019); (c) загрузчик получил `202` с `retry_after: 8`, затем `cancel()` (смена списка) →
  после 8000 мс повторный `fetchFn` не вызывается и `onState` молчит (FR-019a, сценарий 6 US3);
  (d) домен успешно загружен одним загрузчиком → новый загрузчик того же домена с тем же `memo`
  отдаёт результат без `fetchFn` (FR-015, сценарий 5 US3).

### Implementation for User Story 3

- [ ] T026 [US3] Добавить `<th>Веб-архив</th>` в `<thead>` `#top-table` в `templates/index.html`;
  в `loadTop()` в каждую строку четвёртую ячейку: `el('a', {className: 'wayback-link', href:
  WaybackCore.archiveUrl(item.domain), text: 'Открыть в веб-архиве', attrs: {target: '_blank',
  rel: 'noopener noreferrer'}})` и `div.wayback-history` (`role="status"`, `aria-live="polite"`),
  заполненный `renderHistory`: результат из `waybackMemo.get(item.domain)`, если есть, иначе `idle`
  с `button.wayback-request` «Запросить историю», обработчик которой — `startTopHistory(item, container)`.
  Загрузчики при отрисовке не создаются (FR-018). T024 проходит.
- [ ] T027 [US3] Реализовать в `templates/index.html` `startTopHistory(item, container)`: создаёт
  `createHistoryLoader(item.domain, deps)` с общими `waybackLimiter` и `waybackMemo`, `onState` рисует
  через `renderHistory` в `container`, вызывает `start()`; «Повторить» в строке перезапускает только её
  загрузчик (FR-019). Загрузчики и `dispose()` отсчётов хранятся в массиве `topLoaders`; в начале
  `loadTop()` для всех прежних вызываются `cancel()`/`dispose()` и массив очищается (FR-014, FR-019a).
  T025 проходит. Ручная проверка: шаг 3 `quickstart.md` (SC-004).

**Checkpoint**: все четыре истории работают независимо.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T028 Прогнать `python scripts/check.py` на машине с node: все тесты проходят и пропущенных
  тестов 0 — итоговая строка unittest в выводе `OK` без `(skipped=…)`; при любом пропуске задача
  не закрыта. Старые тесты (`tests/test_main.py`, `tests/test_search.py`, `tests/test_mcp_whois.py`,
  `tests/test_skills.py`) не сломаны.
- [ ] T029 Пройти ручные шаги 1–5 из `specs/001-wayback-ui/quickstart.md` в браузере; для шагов 2, 3, 5 —
  на эндпоинте `specs/002-wayback-api`, если он готов, иначе через DevTools Overrides по
  `specs/wayback-contract.md`. Сверить вид сетки и Топ-20 до/после (SC-007).
- [ ] T030 Код-ревью изменений `templates/index.html` и `tests/test_wayback_ui.py` скиллом `code-review`
  с акцентом на принципы IV (только `/api/`) и V (нет HTML-вставок, `href` только через `safeHref`/
  `archiveUrl`); исправить найденное.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: без зависимостей.
- **Foundational (Phase 2)**: после Setup; блокирует все истории.
- **US1 (Phase 3)**: после Phase 2.
- **US2 (Phase 4)**: после Phase 2 и US1 (блок истории живёт в диалоге US1).
- **US4 (Phase 5)**: после Phase 2; от US1/US2 не зависит — можно выполнять сразу после Phase 2.
- **US3 (Phase 6)**: после US2 (загрузчик, `renderHistory`, общие limiter/memo) и US4 (безопасная строка).
- **Polish (Phase 7)**: после всех историй.

### Within Each User Story

- Тест → убедиться, что падает → реализация → тест проходит.
- В ядре: форматирование → `classifyResponse` → memo/limiter → `createHistoryLoader` → DOM.

### Parallel Opportunities

Тесты и реализация лежат в двух разных файлах, но реализация зависит от тестов (TDD), а все задачи
реализации — в одном `templates/index.html`. Поэтому меток [P] нет: параллельная правка одного файла
даёт конфликты. Реальная параллельность — между историями после Phase 2: US4 (T022–T023) может идти
одновременно с US1 (T005–T010), если правки `index.html` сливаются вручную.

---

## Parallel Example: после Phase 2

```bash
# Исполнитель A: US1 (диалог, renderDomains)
Task: "T005–T010 в tests/test_wayback_ui.py и templates/index.html (renderDomains, dialog)"

# Исполнитель B: US4 (безопасная строка Топ-20) — другая функция того же файла
Task: "T022–T023 в tests/test_wayback_ui.py и templates/index.html (loadTop)"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1 → Phase 2.
2. Phase 3 (US1): ссылка «Открыть в веб-архиве» в диалоге — работает без сервера.
3. **STOP and VALIDATE**: `python scripts/check.py` + шаг 1 `quickstart.md`.

### Incremental Delivery

1. Setup + Foundational.
2. US1 → MVP.
3. US4 → закрывает долг по принципу V (не зависит от сервера).
4. US2 → история в диалоге (проверяется на подменённых ответах, если сервера ещё нет).
5. US3 → история по кнопке в каждой строке Топ-20.

---

## Notes

- Серверный эндпоинт `/api/wayback` реализует команда `specs/002-wayback-api`; формат — только по
  `specs/wayback-contract.md`. Если формат в контракте изменится (новая версия), сначала обновить
  `spec.md`/`data-model.md`, затем тесты T012/T014.
- `.specify/feature.json` общий для обеих команд: перед командами Spec Kit проверять, что он указывает
  на `specs/001-wayback-ui`.
- Коммит после каждой задачи или логической группы; на каждом checkpoint `python scripts/check.py`
  должен быть зелёным.
