# Data Model: История домена в веб-архиве (сервер)

Формы ответов API заданы в `specs/wayback-contract.md`; здесь — сущности, их поля, правила
и переходы состояний внутри сервера. Решения — в [research.md](research.md).

## Модели ответа (`models.py`, Pydantic)

### WaybackSnapshot

| Поле | Тип | Правила |
|------|-----|---------|
| `timestamp` | `str` | ISO 8601 UTC с `Z`, из 14-значной отметки CDX (`20090315123456` → `2009-03-15T12:34:56Z`) |
| `date` | `str` | `ГГГГ-ММ-ДД`, дата той же отметки в UTC, без сдвига поясов (FR-008a) |
| `url` | `str` | `https://web.archive.org/web/<14 цифр>/<original>`; `original` — как вернул архив (FR-008) |

### WaybackHistory (HTTP 200)

| Поле | Тип | Правила |
|------|-----|---------|
| `domain` | `str` | Отображаемая форма (`.рф` кириллицей), FR-004 |
| `snapshot_count` | `int` | ≥ 0; число валидных строк CDX; 100 000 при усечении |
| `count_truncated` | `bool` | `true` ⇔ строк ровно 100 000 (FR-009) |
| `first` | `WaybackSnapshot \| None` | Минимальная отметка; `None` ⇔ `snapshot_count == 0` |
| `last` | `WaybackSnapshot \| None` | Максимальная отметка (при усечении — из отдельного запроса); `None` ⇔ `snapshot_count == 0` |
| `cached` | `bool` | Выставляется при выдаче: `true` для ответа из кэша; в кэше хранится как `false` |
| `checked_at` | `str` | ISO 8601 UTC с `Z` — момент получения от архива; из кэша — исходный момент |

Инварианты: `first.timestamp ≤ last.timestamp`; при `snapshot_count == 1` `first == last`.

### WaybackPending (HTTP 202)

| Поле | Тип | Правила |
|------|-----|---------|
| `domain` | `str` | Отображаемая форма |
| `status` | `Literal["pending"]` | Всегда `pending` |
| `retry_after` | `int` | ≥ 1; совпадает с заголовком `Retry-After` (FR-019a) |

### Тело ошибки (HTTP 400/502/503/504)

`{"code": str, "detail": str}`; `code` ∈ `invalid_domain`, `archive_overloaded`, `archive_timeout`,
`archive_error`. Формируется из исключения, отдельная Pydantic-модель не обязательна.

## Внутренние сущности (`wayback.py`)

### DomainName

Результат нормализации (R3).

| Поле | Тип | Правила |
|------|-----|---------|
| `display` | `str` | Нижний регистр, без завершающей точки, `.рф` кириллицей; ключ кэша и задач |
| `ascii` | `str` | Punycode-форма для запроса к архиву; ≤ 253 символа; метки `[a-z0-9-]{1,63}` без дефиса по краям |

Ошибка проверки → `InvalidDomain`. Зоны: `ru`, `su`, `xn--p1ai` (`рф`). Ровно две метки — только
домены второго уровня; поддомены, включая `www.`, отклоняются (FR-003).

### WaybackSettings (неизменяемый dataclass)

| Поле | По умолчанию | Требование |
|------|--------------|------------|
| `request_interval` | 1.0 с | FR-017 |
| `response_deadline` | 15.0 с | FR-019 |
| `archive_timeout` | 10.0 с | FR-021 |
| `max_queue` | 100 | FR-020 |
| `cache_ttl` | 604 800 с (7 дней) | FR-012 |
| `cache_max_entries` | 10 000 | FR-016 |
| `max_rows` | 100 000 | FR-006, FR-009 |
| `pause_default` | 60 с | FR-022 |
| `pause_max` | 600 с | FR-022 (предел удвоения, когда архив не прислал `Retry-After`) |

Границы `Retry-After` архива — константы модуля, а не настройки: `ARCHIVE_RETRY_AFTER_MIN = 1`,
`ARCHIVE_RETRY_AFTER_MAX = 600` (FR-022).

### CacheEntry

| Поле | Тип | Правила |
|------|-----|---------|
| `history` | `WaybackHistory` | С `cached = false` |
| `checked_at` | `float` | Unix-время получения; свежая ⇔ `now - checked_at < cache_ttl` |

Файл: `{"version": 1, "entries": {"<display>": {"checked_at": <float>, "history": {...}}}}`.
Неизвестная версия, битый JSON или запись, не прошедшая валидацию, — игнорируются с предупреждением.

### Job (задача очереди)

| Поле | Тип | Правила |
|------|-----|---------|
| `domain` | `DomainName` | Один `Job` на `display` (FR-018) |
| `future` | `asyncio.Future[WaybackHistory]` | Заменяется новым после «429», если задача возвращается в очередь |
| `requeued_after_429` | `bool` | Не более одного возврата (FR-022) |
| `enqueued_at` | `float` | Монотонное время постановки |

### QueueState

| Поле | Тип | Смысл |
|------|-----|-------|
| `pending` | `deque[Job]` | Ожидающие задачи, FIFO; длина ≤ `max_queue` |
| `jobs` | `dict[str, Job]` | Все задачи в очереди и выполняемая, по `display` |
| `current` | `Job \| None` | Выполняемая задача |
| `current_started` | `float` | Монотонное время начала обращения (для оценки, R5) |
| `last_start` | `float` | Начало последнего обращения к архиву (интервал) |
| `pause_until` | `float` | Конец паузы после «429»; `0` — паузы нет |
| `last_pause` | `float` | Длительность последней паузы (для удвоения); сбрасывается при успехе |
| `loop` | `AbstractEventLoop \| None` | Цикл, к которому привязаны работник, событие и `Future`; `None` до первого запроса |
| `wakeup` | `asyncio.Event \| None` | «Есть работа» для работника; создаётся лениво |
| `worker` | `asyncio.Task \| None` | Задача-работник; создаётся лениво, перезапускается, если завершилась |

`QueueState` — состояние выполнения: при смене цикла событий оно целиком сбрасывается (отмена
работника, очистка `pending`/`jobs`/паузы), а кэш сохраняется (R10).

## Исключения

| Класс | `code` | HTTP | `Retry-After` |
|-------|--------|------|---------------|
| `InvalidDomain` | `invalid_domain` | 400 | — |
| `ArchiveOverloaded` | `archive_overloaded` | 503 | обязателен |
| `ArchiveTimeout` | `archive_timeout` | 504 | — |
| `ArchiveUnavailable` | `archive_error` | 502 | — |
| `HistoryPending` | — (`status: pending`) | 202 | обязателен |

Первые четыре наследуют `WaybackError`; `HistoryPending` — отдельный класс, это не ошибка.

## Переходы состояний запроса истории

```text
запрос ──нормализация──✗──► 400 invalid_domain
   │
   ├─ свежая запись в кэше ──────────────────────────► 200 (cached=true)
   ├─ идёт пауза после 429 ── поставить/найти задачу ─► 503 archive_overloaded (Retry-After = остаток паузы)
   ├─ задачи нет и очередь полна ────────────────────► 503 archive_overloaded
   └─ найти или поставить задачу
         ├─ оценка > остатка срока ──────────────────► 202 pending
         └─ ждать Future до срока
               ├─ успех / нет снимков ───────────────► 200 (cached=false)
               ├─ 429 / таймаут / ошибка архива ─────► 503 / 504 / 502
               └─ срок истёк ────────────────────────► 202 pending (задача продолжается)
```

Задача: `в очереди → выполняется → {готово (кэш) | 429: в начало очереди (один раз) или снята |
таймаут/ошибка: снята}`. Снятая задача удаляется из `jobs`; следующий запрос домена создаёт новую.
