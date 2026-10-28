# Contract: `GET /api/wayback` (серверная сторона)

**Источник истины по внешнему формату** — `specs/wayback-contract.md` v1.1. Этот файл не повторяет
форматы, а фиксирует, как сервер их выполняет: внутренний интерфейс сервиса и матрицу соответствия,
по которой пишутся контрактные тесты (`tests/test_wayback_api.py`).

## Внешний интерфейс

```text
GET /api/wayback?domain=<домен>
→ 200 WaybackHistory | 202 WaybackPending + Retry-After | 400/502/503/504 {code, detail} [+ Retry-After для 503]
```

- Параметр `domain` объявлен как `str = ""`: отсутствующий параметр — `400 invalid_domain`,
  а не `422` FastAPI.
- `Content-Type: application/json` во всех ответах.
- Заголовок `Retry-After` — целые секунды, равен `retry_after` в теле `202`.

## Внутренний интерфейс сервиса

```text
services.get_wayback_history(domain: str) -> WaybackHistory        (async; реэкспорт из wayback.py)
  raises InvalidDomain | ArchiveOverloaded | ArchiveTimeout | ArchiveUnavailable | HistoryPending

wayback.WaybackService(settings: WaybackSettings, fetcher: Fetcher, clock: Callable[[], float],
                       cache_path: Path | None)
  .get_history(domain: str) -> WaybackHistory                        (async)
  конструктор не создаёт asyncio-объектов; они создаются при первом get_history в работающем цикле

wayback.get_service() -> WaybackService                              (ленивый модульный экземпляр)
wayback.reset_service(service: WaybackService | None = None) -> None (тесты: сброс/подмена)
wayback.shutdown_service() -> None                                   (async; из shutdown FastAPI)

Fetcher = async (ascii_domain: str, limit: int) -> FetchResult(status: int, headers: Mapping, text: str)
  limit > 0  — первые N снимков; limit = -1 — последний снимок (+ fastLatest=true)
  raises httpx.TimeoutException | httpx.RequestError

wayback.make_cdx_fetcher(timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> Fetcher
  загрузчик по умолчанию: httpx.AsyncClient(timeout=timeout, transport=transport) на каждый вызов;
  transport — только для тестов (httpx.MockTransport)
```

Эндпоинт не содержит логики, кроме перевода результата и исключений в ответ.

## Матрица соответствия (контрактные тесты эндпоинта)

| # | Поведение сервиса (мок) | HTTP | Тело | `Retry-After` |
|---|-------------------------|------|------|---------------|
| C1 | `WaybackHistory` с 1234 снимками | 200 | все поля `WaybackHistory`, `date` = `ГГГГ-ММ-ДД` | нет |
| C2 | `WaybackHistory` с 0 снимков | 200 | `snapshot_count: 0`, `first: null`, `last: null` | нет |
| C3 | `HistoryPending(domain, 8)` | 202 | `{"domain", "status": "pending", "retry_after": 8}` | `8` |
| C4 | `InvalidDomain` | 400 | `code: invalid_domain`, непустой `detail` | нет |
| C5 | параметр `domain` не передан | 400 | `code: invalid_domain` | нет |
| C6 | `ArchiveOverloaded(retry_after=60)` | 503 | `code: archive_overloaded` | `60` |
| C7 | `ArchiveTimeout` | 504 | `code: archive_timeout` | нет |
| C8 | `ArchiveUnavailable` | 502 | `code: archive_error` | нет |
| C9 | любое `WaybackError` | — | в теле только ключи `code` и `detail`, без стектрейса | — |
| C10 | `пример.рф` и `xn--e1afmkfd.xn--p1ai` | — | сервис вызывается с исходной строкой; нормализация — в сервисе | — |
| C12 | любой исход (200, 202, 400, 502, 503, 504) | — | заголовок `Content-Type` начинается с `application/json`, тело разбирается как JSON | — |
| C13 | `with TestClient(app)` (старт/остановка приложения, `mcp_client` подменён) | — | после выхода из контекста работник модульного сервиса завершён (`shutdown` вызвал `shutdown_service`) | — |
| C11 | настоящий `WaybackService` + фейковый загрузчик (`reset_service(...)`), два запроса через `TestClient` без `with` | 200, 200 | второй ответ `cached: true`, загрузчик вызван один раз, нет `RuntimeError` о чужом цикле | нет |

Между тестами эндпоинта модульный сервис сбрасывается `reset_service()` в `setUp`/`tearDown` (R10).

## Матрица поведения сервиса (тесты `tests/test_wayback.py`)

| Группа | Проверяется | Требования |
|--------|-------------|------------|
| Нормализация | регистр, точка, punycode ↔ кириллица, зоны, только второй уровень (поддомены и `www.` → 400), длина, запрещённые символы | FR-002–FR-004 |
| Загрузчик CDX | адрес и параметры запроса (`url`, `fl`, `limit`, `fastLatest` при `limit=-1`), таймаут клиента = `archive_timeout`, проброс статуса/заголовков/тела — через `httpx.MockTransport` | FR-005–FR-007, FR-021 |
| Разбор CDX | счёт, первый/последний, URL снимка, `date` в UTC на границе суток, пустой ответ, битые строки, 403 «исключён», усечение 100 000 + запрос последнего | FR-005–FR-011 |
| Кэш | попадание без обращения, «нет снимков» кэшируется, ошибки — нет, TTL 7 дней, один ключ для кириллицы/punycode, файл переживает перезапуск, битый файл, вытеснение > 10 000 | FR-012–FR-016 |
| Очередь | один одновременный вызов, интервал между началами, объединение одинаковых доменов, 202 по сроку и по оценке, фоновая доработка в кэш, полная очередь → 503 | FR-017–FR-020, FR-019a, FR-019b |
| Ошибки архива | таймаут → 504 без повтора, сеть/5xx/мусор → 502, «429» → 503 + пауза, удвоение до 600 с, сброс после успеха, возврат в начало один раз, во время паузы кэш работает | FR-021–FR-024, FR-026 |
