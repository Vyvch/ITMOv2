# UI Contract: История домена в веб-архиве

HTTP-обмен с сервером — только по `specs/wayback-contract.md` v1.1 (`GET /api/wayback?domain=`);
здесь он не повторяется. Этот файл фиксирует то, на что опираются тесты интерфейса:
программный интерфейс `WaybackCore` и якоря DOM в `templates/index.html`.

## 1. `WaybackCore` (блок `<script id="wayback-core">`)

Блок не обращается к DOM и глобальным `fetch`/таймерам напрямую — только через `deps`.
Из глобальной области берёт только `URL`, `Intl` и `AbortController` (в браузере есть; тестовая
песочница `vm` передаёт `URL` и `AbortController` явно). Публикует `globalThis.WaybackCore`.

| Функция | Вход → выход |
|---------|--------------|
| `archiveUrl(domain)` | `'пример.рф'` → `'https://web.archive.org/web/*/xn--e1afmkfd.xn--p1ai'`; `'Example.RU'` → `'https://web.archive.org/web/*/example.ru'` |
| `formatDate(text)` | `'2009-03-15'` → `'15.03.2009'`; не `ГГГГ-ММ-ДД` → `'—'` |
| `formatCount(count, truncated)` | форматирует `count`, при `truncated` добавляет `'+'`: `(1234, false)` → `'1 234'`; `(100000, true)` → `'100 000+'` (разделитель U+00A0) |
| `safeHref(url)` | `https://web.archive.org/...` → строка адреса; `http:`, другой хост, иная схема, пустое, не URL → `null` |
| `createCountdown(seconds, deps, onTick)` | → `{ cancel() }`; `onTick(seconds)` сразу, затем каждые 1000 мс до `onTick(0)`; таймеры через `deps.setTimer`/`deps.clearTimer` |
| `classifyResponse(status, retryAfterHeader, bodyText)` | → `WaybackResult` (см. data-model.md) |
| `createLimiter(max)` | → `{ run(task, signal) }`; не более `max` задач одновременно, FIFO |
| `createHistoryLoader(domain, deps)` | → `{ start(), cancel() }` |
| `createMemo()` | → `{ get(domain), put(domain, result) }`; хранит только `success`/`empty` |

`deps` для `createHistoryLoader`:

| Поле | Назначение |
|------|------------|
| `fetchFn(url, {signal})` | возвращает `{status, headers.get(name), text()}` |
| `now()` | миллисекунды |
| `setTimer(fn, ms)` / `clearTimer(id)` | таймеры |
| `limiter` | общий `createLimiter(4)` страницы |
| `memo` | общий `createMemo()` страницы |
| `onState(result)` | вызывается с `{kind: 'loading'}` и затем с итоговым `WaybackResult` |

Гарантии `createHistoryLoader`:
- `start()` при наличии записи в `memo` сразу отдаёт её, без запроса.
- Запрос: `/api/wayback?domain=` + `encodeURIComponent(domain)`.
- `pending` → повтор не раньше `retryAfterSec`; итог не позже 120 000 мс от `start()`.
- Запрос без ответа 20 000 мс от фактической отправки → `timeout` (`reason: 'request'`).
- Слот `limiter` занимается на один запрос и освобождается сразу после ответа или таймаута;
  ожидание повтора после `pending` слот не держит.
- После `cancel()` `onState` больше не вызывается для этой попытки.

Константы: `DEADLINE_MS = 120000`, `REQUEST_TIMEOUT_MS = 20000`, `MAX_PARALLEL = 4`.

## 2. Якоря DOM

| Элемент | Назначение |
|---------|------------|
| `button.domain-card` в `#domain-grid` | карточка в сетке «Все домены», открывает диалог |
| `dialog#domain-dialog` | карточка домена; `aria-labelledby="domain-dialog-title"` |
| `#domain-dialog-title` | имя домена |
| `#domain-dialog-date` | дата освобождения |
| `a.wayback-link` | «Открыть в веб-архиве», `target="_blank" rel="noopener noreferrer"` |
| `.wayback-history[role="status"]` | блок истории (в диалоге и в каждой строке Топ-20) |
| `button.wayback-request` | «Запросить историю» (только диалог) |
| `button.wayback-retry` | «Повторить» / «Повторить через N с» |
| `button.domain-dialog-close` | «Закрыть» |
| `th` «Веб-архив» в `#top-table` | новая колонка Топ-20 |

## 3. Тексты интерфейса

«Открыть в веб-архиве», «Запросить историю», «Загрузка истории…», «Снимков: N», «Первый: ДД.ММ.ГГГГ»,
«Последний: ДД.ММ.ГГГГ», «Нет снимков в веб-архиве», «Веб-архив перегружен, попробуйте позже»,
«Веб-архив не ответил вовремя», «Не удалось получить историю», «Повторить», «Повторить через N с»,
«Закрыть».

## 4. Запреты (проверяются статическими тестами)

- Нет `outerHTML`, `insertAdjacentHTML`, `document.write`; `innerHTML` — только `= ''`.
- `fetch(` — только относительные адреса `/api/...`.
- Каждый подставляемый в адрес `fetch` параметр обёрнут в `encodeURIComponent(...)`: в шаблонной
  строке — только `${encodeURIComponent(...)}`, при конкатенации — `+ encodeURIComponent(...)`
  (принцип V).
