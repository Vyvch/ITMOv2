---

description: "Задачи реализации: история домена в веб-архиве (сервер)"
---

# Tasks: История домена в веб-архиве (сервер)

**Input**: Design documents from `specs/002-wayback-api/` + общий контракт `specs/wayback-contract.md`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/wayback-api.md, quickstart.md

**Tests**: обязательны — принцип II конституции (Red → Green → Refactor, `unittest`, без сети).
В каждой истории тесты пишутся первыми и должны упасть до реализации.

**Organization**: задачи сгруппированы по пользовательским историям spec.md (US1–US4).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: можно делать параллельно (другой файл, нет зависимости от незавершённых задач)
- **[Story]**: история из spec.md (US1, US2, US3, US4)
- Плоская структура проекта: модули в корне (`wayback.py`, `api.py`, `services.py`, `models.py`),
  тесты в `tests/`

## Общие правила для всех задач

- Стиль: PEP 8, аннотации типов у публичных функций, `logger = logging.getLogger(__name__)`,
  комментарии и docstring — на русском, идентификаторы — на английском.
- Тесты: `unittest`, шапка с `sys.path.insert(...)` как в `tests/test_search.py`; асинхронные —
  `unittest.IsolatedAsyncioTestCase`; настройки сервиса в тестах — малые (`request_interval=0.01`,
  `response_deadline=0.2`, `archive_timeout=0.1`, `pause_default=0.05`, `pause_max=0.2`), чтобы
  прогон занимал секунды.
- Никаких `asyncio`-объектов и `httpx.AsyncClient` на уровне модуля (research R10).
- Тесты, поднимающие приложение целиком (`with TestClient(app)`), подменяют `api.mcp_client`
  (`unittest.mock.patch`), чтобы `startup` не запускал подпроцесс MCP.
- Не менять `docs/requirements.md` и `scripts/check.py`.
- После каждой задачи с кодом: `python scripts/check.py` → код 0 (кроме намеренно красных тестов).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: реальные образцы ответов архива и заготовки модулей

- [X] T001 Один раз вручную снять реальные ответы Wayback CDX и сохранить как образцы в `tests/fixtures/wayback/` (единственная задача с сетью, кроме ручной проверки T034; research R1). Запрос — ровно тот, что будет в коде: `https://web.archive.org/cdx/search/cdx?url=<домен>&fl=timestamp,original&limit=100000` (например, `curl -sS -D headers.txt -o cdx_regular.txt "<адрес>"`). Сохранить: `tests/fixtures/wayback/cdx_regular.txt` — обычный домен `.ru` второго уровня с десятками–сотнями снимков (если файл > ~200 КБ — выбрать домен поменьше); `tests/fixtures/wayback/cdx_empty.txt` — домен без снимков (например, случайное `zq7x9k2m4p8w.ru`; ожидается пустое тело при 200); `tests/fixtures/wayback/capture.md` — дата съёмки, оба точных адреса, HTTP-статусы, значимые заголовки (`Content-Type`, `Retry-After` если есть), имена доменов, число строк и первая/последняя строка обычного образца. Если попадётся сайт, исключённый из архива, — сохранить `tests/fixtures/wayback/cdx_blocked.txt` со статусом в `capture.md`. Сверить формат строк с допущениями research R1–R2 и при расхождении поправить `specs/002-wayback-api/research.md` до начала T015
- [X] T002 [P] Создать `wayback.py`: docstring модуля, `logger = logging.getLogger(__name__)`, неизменяемый `@dataclass(frozen=True) WaybackSettings` с полями и значениями по умолчанию из data-model: `request_interval: float = 1.0`, `response_deadline: float = 15.0`, `archive_timeout: float = 10.0`, `max_queue: int = 100`, `cache_ttl: float = 604800`, `cache_max_entries: int = 10000`, `max_rows: int = 100000`, `pause_default: float = 60`, `pause_max: float = 600`; константы `CDX_URL = "https://web.archive.org/cdx/search/cdx"`, `SNAPSHOT_URL_PREFIX = "https://web.archive.org/web/"`, `ARCHIVE_RETRY_AFTER_MIN = 1`, `ARCHIVE_RETRY_AFTER_MAX = 600`; `@dataclass FetchResult(status: int, headers: Mapping[str, str], text: str)` и тип `Fetcher = Callable[[str, int], Awaitable[FetchResult]]`
- [X] T003 [P] Создать `tests/test_wayback.py` с общими помощниками (без тестов). Импорт `wayback` и `models` — только внутри функций и методов помощников, а не на уровне модуля: тогда файл импортируется и `scripts/check.py` не падает до появления `WaybackService` (T007). Помощники: `FakeFetcher` — асинхронный вызываемый объект со сценарием ответов по домену (значение `FetchResult`, исключение или задержка `asyncio.sleep`), журналом вызовов `(ascii_domain, limit, monotonic_start)` и счётчиком; `FakeClock` — `__call__() -> float` с методом `advance(seconds)`; `load_fixture(name) -> str` из `tests/fixtures/wayback/`; `make_settings(**overrides)` с малыми тестовыми значениями из «Общих правил»; `make_service(tmpdir, fetcher, clock=None, **settings)` с `cache_path` во временном каталоге

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: модели ответа, исключения, имя домена и жизненный цикл сервиса с ленивой привязкой к циклу событий

**⚠️ CRITICAL**: истории не начинаются, пока фаза не завершена

- [X] T004 [P] Добавить в `models.py` Pydantic-модели (data-model): `WaybackSnapshot(timestamp: str, date: str, url: str)` — «`timestamp` ISO 8601 UTC с `Z`», «`date` `ГГГГ-ММ-ДД`, дата той же отметки в UTC, без сдвига поясов», «`url` `https://web.archive.org/web/<14 цифр>/<original>`»; `WaybackHistory(domain: str, snapshot_count: int, count_truncated: bool, first: WaybackSnapshot | None, last: WaybackSnapshot | None, cached: bool, checked_at: str)` — «`snapshot_count` ≥ 0», «`first`/`last` `None` ⇔ `snapshot_count == 0`», «`checked_at` ISO 8601 UTC с `Z`»; `WaybackPending(domain: str, status: Literal["pending"] = "pending", retry_after: int)` — «`retry_after` ≥ 1»
- [X] T005 [P] Добавить в `wayback.py` (data-model, research R7): `@dataclass(frozen=True) DomainName(display: str, ascii: str)` — «`display` нижний регистр, без завершающей точки, `.рф` кириллицей; ключ кэша и задач», «`ascii` punycode-форма для запроса к архиву» (функция нормализации — в T014); исключения: базовый `WaybackError(Exception)` с атрибутами `code: str`, `status: int`, `detail: str`, `retry_after: int | None`; `InvalidDomain` (`invalid_domain`, 400, «Некорректный домен»), `ArchiveOverloaded` (`archive_overloaded`, 503, «Веб-архив перегружен, попробуйте позже», `retry_after` обязателен), `ArchiveTimeout` (`archive_timeout`, 504, «Веб-архив не ответил вовремя»), `ArchiveUnavailable` (`archive_error`, 502, «Не удалось получить историю из веб-архива»); отдельный класс `HistoryPending(Exception)` (не наследник `WaybackError`) с `domain: str` — всегда отображаемая форма `DomainName.display` — и `retry_after: int` (≥ 1), `status = 202`
- [X] T006 Написать падающие тесты жизненного цикла в `tests/test_wayback.py`, класс `TestServiceLifecycle` (research R10, plan R-1/R-2/R-5); пункты (в) и (г) дополнительно проверяют те же функции через реэкспорт `services.reset_service`/`services.shutdown_service` (T008): (а) `import wayback` и `WaybackService(...)` вне цикла событий не создают `asyncio`-объектов — у свежего сервиса `loop`, `wakeup`, `worker` равны `None`; (б) `asyncio.run(service.get_history("example.ru"))` дважды подряд на одном экземпляре (два разных цикла) — оба вызова завершаются без `RuntimeError` и без зависания (обёртка `asyncio.wait_for(..., 2)`), во втором цикле `service.loop` — новый; (в) `wayback.reset_service(custom)` подставляет экземпляр, который возвращает `wayback.get_service()`, `reset_service()` сбрасывает в `None` и следующий `get_service()` создаёт новый; (г) `await wayback.shutdown_service()` отменяет работника, `worker.done()` истинно; (д) если работник завершился, следующий `get_history` перезапускает его. Для изоляции от разбора CDX тесты используют подкласс `WaybackService` с переопределённым `_execute(job)`, возвращающим готовую `WaybackHistory`
- [X] T007 Реализовать в `wayback.py` каркас `WaybackService(settings: WaybackSettings, fetcher: Fetcher, clock: Callable[[], float] = time.time, cache_path: Path | None = None)` (research R4, R10; data-model `Job`, `QueueState`): конструктор без `asyncio`-объектов (`deque`, `dict` задач, `loop = wakeup = worker = None`); `_ensure_runtime()` — привязка к `asyncio.get_running_loop()`, при смене цикла `logger.warning` и полный сброс состояния выполнения (отмена старого работника — только если его цикл не закрыт, иначе `cancel()` бросит `RuntimeError: Event loop is closed`; очистка `pending`/`jobs`/паузы/`last_start`), создание `asyncio.Event` и задачи-работника, перезапуск завершившегося работника; `Job(domain: DomainName, future, requeued_after_429=False, enqueued_at)` с `future` из `loop.create_future()` и `add_done_callback`, помечающим исключение прочитанным; работник — цикл «ждать `wakeup` → взять задачу слева → `await self._execute(job)` → завершить `future` результатом или исключением → удалить из `jobs`»; `get_history(domain)` — поставить задачу и дождаться `future` (до T014 — с `DomainName(display=domain, ascii=domain)`); `_execute` — метод-заглушка для US1. Модульные `_service: WaybackService | None = None`, `get_service()`, `reset_service(service: WaybackService | None = None)`, `async shutdown_service()`. Тесты T006 зелёные, кроме проверок через `services` (они — после T008)
- [X] T008 Реэкспортировать из `services.py`: `get_wayback_history` (async-обёртка `get_service().get_history(domain)`), `reset_service`, `shutdown_service` и исключения `WaybackError`, `HistoryPending` (подключение к `shutdown` в `api.py` — в T018, после теста C13 в T013). Тесты T006 полностью зелёные

**Checkpoint**: сервис создаётся лениво, переживает смену цикла и сбрасывается между тестами

---

## Phase 3: User Story 1 — Получить историю домена (Priority: P1) 🎯 MVP

**Goal**: `GET /api/wayback?domain=...` возвращает число снимков, первый и последний снимок или «нет снимков»; некорректный домен → 400.

**Independent Test**: фейковый загрузчик на образцах из `tests/fixtures/wayback/`; эндпоинт отдаёт 200 с верными полями, 200 с нулём снимков и 400 для некорректного домена без обращения к архиву.

### Tests for User Story 1 ⚠️ (пишутся первыми, должны упасть)

- [X] T009 [US1] Тесты нормализации `normalize_domain(raw: str) -> DomainName` в `tests/test_wayback.py`, класс `TestNormalizeDomain` (FR-002–FR-004, research R3): `"Example.RU"`, `" example.ru "`, `"example.ru."` → `display == ascii == "example.ru"`; `"пример.рф"`, `"ПРИМЕР.РФ"`, `"xn--e1afmkfd.xn--p1ai"`, `"XN--E1AFMKFD.XN--P1AI"` → `display == "пример.рф"`, `ascii == "xn--e1afmkfd.xn--p1ai"`; `"shop.su"` принимается; `InvalidDomain` для `""`, `"   "`, `"http://example.ru/page"`, `"example.ru/"`, `"example.ru:80"`, `"user@example.ru"`, `"exa mple.ru"`, `"example.com"`, `"ru"`, `"a..ru"`, `"-abc.ru"`, `"abc-.ru"`, метки из 64 символов, имени длиннее 253 символов, `"exa_mple.ru"`; поддомены (только второй уровень, FR-003): `"www.example.ru"`, `"WWW.Example.RU."`, `"shop.msk.ru"`, `"a.b.example.ru"`, `"www.пример.рф"`, `"www.xn--e1afmkfd.xn--p1ai"`, `"m.shop.su"`
- [X] T010 [US1] Тесты разбора CDX в `tests/test_wayback.py`, класс `TestParseCdx` (FR-006, FR-008, FR-008a, FR-010, FR-011): на `cdx_regular.txt` — `snapshot_count` равен числу строк, `first`/`last` — первая/последняя строки из `capture.md`, `url` = `https://web.archive.org/web/<14 цифр>/<original>`; синтетика `20091231233000 http://example.ru/` → `timestamp == "2009-12-31T23:30:00Z"`, `date == "2009-12-31"`; одна строка → `first == last`, счёт 1; `cdx_empty.txt` и пустая строка → 0 снимков, `first is None`, `last is None`; строки с битой отметкой (`2009`, `abcdefghijklmn`, `20091331000000`) пропускаются с `assertLogs(level="WARNING")`; только битые строки → `ArchiveUnavailable`
- [X] T011 [US1] Тесты сервиса для US1 в `tests/test_wayback.py`, класс `TestGetHistoryUS1` (FR-003, FR-006, FR-007, FR-009, FR-010): `FakeFetcher` с `cdx_regular.txt` → `WaybackHistory` с `cached=False` и `checked_at` в формате ISO 8601 `Z`; `пример.рф` → загрузчик вызван с `"xn--e1afmkfd.xn--p1ai"` и `limit == settings.max_rows`, в ответе `domain == "пример.рф"`; некорректный домен и `www.example.ru` → `InvalidDomain`, загрузчик не вызван; ответ 403 с телом `cdx_blocked.txt` (или синтетическим, содержащим `AdministrativeAccessControlException: Blocked Site Error`) → 0 снимков и `assertLogs(level="INFO")` с пометкой об исключении; усечение при `max_rows=5` и 5 строках → `count_truncated=True`, `snapshot_count == 5`, второй вызов загрузчика с `limit == -1`, `last` из второго ответа; усечение, когда второй вызов (`limit == -1`) отвечает 500 → весь запрос завершается `ArchiveUnavailable`, частичный результат не возвращается (spec Edge Cases, FR-009)
- [X] T012 [US1] Тесты загрузчика `make_cdx_fetcher(timeout, transport=None)` в `tests/test_wayback.py`, класс `TestCdxFetcher` (FR-005–FR-007, FR-021; research R1, R4), без сети — `httpx.MockTransport(handler)`, где `handler` запоминает запросы: вызов `fetcher("xn--e1afmkfd.xn--p1ai", 100000)` → один GET на `https://web.archive.org/cdx/search/cdx` с параметрами `url=xn--e1afmkfd.xn--p1ai`, `fl=timestamp,original`, `limit=100000` и без `fastLatest`; вызов с `limit=-1` → `limit=-1` и `fastLatest=true`; `request.extensions["timeout"]["read"]` и `["connect"]` равны переданному `timeout` (не 5 с по умолчанию у `httpx`); ответы 200 (текст образца), 429 с `Retry-After: 30` и 403 возвращаются как `FetchResult` с тем же статусом, заголовками и телом без интерпретации; исключение `httpx.ConnectError` из транспорта пробрасывается
- [X] T013 [P] [US1] Тесты эндпоинта в новом `tests/test_wayback_api.py` (contracts C1, C2, C4, C5, C9, C10, C13 и C11 без кэша): класс с `setUp`/`tearDown`, вызывающими `services.reset_service()`; `patch("api.get_wayback_history", new_callable=AsyncMock)`; C1 — 200 и все поля `WaybackHistory`, `date` по шаблону `^\d{4}-\d{2}-\d{2}$`; C2 — `snapshot_count: 0`, `first: null`, `last: null`; C4 — `InvalidDomain` → 400, тело ровно с ключами `code`, `detail`, `code == "invalid_domain"`; C5 — `GET /api/wayback` без параметра → 400 `invalid_domain` (не 422); C10 — мок вызван с исходной строкой `пример.рф`; C11 (вариант US1) — настоящий `WaybackService` с фейковым загрузчиком через `reset_service(...)`, два запроса подряд через `TestClient(app)` без `with` → оба 200, нет `RuntimeError` о чужом цикле; C13 — с подменённым `api.mcp_client`: `with TestClient(app) as client:` запрос с настоящим сервисом и фейковым загрузчиком → 200, после выхода из блока `wayback.get_service().worker.done()` истинно (обработчик `shutdown` вызвал `shutdown_service`; подключается в T018)

### Implementation for User Story 1

- [X] T014 [US1] Реализовать `normalize_domain(raw: str) -> DomainName` в `wayback.py` по research R3 (класс `DomainName` — из T005): `strip()` → снять одну завершающую точку → `lower()` → отказ при пробелах и символах `/ : @ ? #` → `name.encode("idna").decode("ascii")` → «метки `[a-z0-9-]{1,63}` без дефиса по краям», «≤ 253 символа», «ровно две метки» (только второй уровень, поддомены и `www.` → `InvalidDomain`), зоны `ru`, `su`, `xn--p1ai` → `display = ascii.encode("ascii").decode("idna")`; любая `UnicodeError` → `InvalidDomain`. Тесты T009 зелёные
- [X] T015 [US1] Реализовать разбор в `wayback.py`: `parse_cdx(text: str) -> list[tuple[str, str]]` (строки `<14 цифр> <original>`; отметка валидна, если ровно 14 цифр и `datetime.strptime(..., "%Y%m%d%H%M%S")` проходит; иначе `logger.warning` и пропуск; непустой текст без валидных строк → `ArchiveUnavailable`) и `make_snapshot(timestamp: str, original: str) -> WaybackSnapshot` (ISO с `Z`, `date` в UTC, `url = SNAPSHOT_URL_PREFIX + timestamp + "/" + original`). Тесты T010 зелёные
- [X] T016 [US1] Реализовать фабрику загрузчика `make_cdx_fetcher(timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> Fetcher` в `wayback.py` (research R4): возвращаемая корутина на каждый вызов открывает `async with httpx.AsyncClient(timeout=timeout, transport=transport) as client` (явный таймаут вместо 5 с `httpx` по умолчанию, FR-021); `GET CDX_URL` с параметрами `url`, `fl=timestamp,original`, `limit`; при `limit == -1` добавить `fastLatest=true`; вернуть `FetchResult` со статусом, заголовками и текстом без интерпретации; `get_service()` создаёт сервис с `make_cdx_fetcher(settings.archive_timeout)`. Тесты T012 зелёные
- [X] T017 [US1] Реализовать `WaybackService._execute(job)` и путь `get_history` для US1 в `wayback.py`: `normalize_domain` до постановки в очередь (`InvalidDomain` без обращения к архиву); вызов загрузчика с `job.domain.ascii` и `settings.max_rows`; 200 + пустое тело → 0 снимков; 403 с признаком `AdministrativeAccessControlException`/`Blocked Site` → 0 снимков + `logger.info`; 200 → `parse_cdx`, `snapshot_count`, `first` = минимальная отметка, `last` = максимальная; если строк ровно `max_rows` — `count_truncated=True` и второй вызов загрузчика с `limit=-1` для `last`, ошибка второго вызова завершает весь запрос этой ошибкой; `checked_at` из `clock()` в ISO 8601 UTC с `Z`; прочие статусы пока → `ArchiveUnavailable` (детали — US2). Тесты T011 зелёные
- [X] T018 [US1] Добавить эндпоинт в `api.py`: `@app.get("/api/wayback") async def api_wayback(domain: str = "")` вызывает `get_wayback_history(domain)` из `services`; успех → `history.model_dump()`; `WaybackError` → `JSONResponse(status_code=exc.status, content={"code": exc.code, "detail": exc.detail}, headers={"Retry-After": str(exc.retry_after)} if exc.retry_after else None)`; `HistoryPending` → `JSONResponse(202, WaybackPending(domain=exc.domain, retry_after=exc.retry_after).model_dump(), headers={"Retry-After": str(exc.retry_after)})`; без `HTTPException` (иначе тело станет `{"detail": {...}}`); в существующий обработчик `shutdown_event` добавить `await shutdown_service()` рядом с `mcp_client.close()`. Тесты T013 (включая C13) зелёные

**Checkpoint**: MVP — история домена отдаётся по контракту для успеха, «нет снимков» и некорректного домена

---

## Phase 4: User Story 2 — Понятные ошибки при сбоях архива (Priority: P2)

**Goal**: «429», таймаут, сетевые ошибки, 5xx и мусор превращаются в 503/504/502 с `code` и `detail`; никаких 500 и стектрейсов; каждое обращение журналируется.

**Independent Test**: фейковый загрузчик по очереди отвечает «429», зависает дольше таймаута, бросает сетевую ошибку, отвечает 500 и мусором; проверяются исключение сервиса, HTTP-статус, `code`, отсутствие стектрейса и запись в журнале.

### Tests for User Story 2 ⚠️

- [ ] T019 [US2] Тесты сервиса в `tests/test_wayback.py`, класс `TestArchiveErrors` (FR-021, FR-022 в части ответа клиенту, FR-024, FR-026, FR-027): значение по умолчанию `WaybackSettings().archive_timeout == 30.0` (FR-021, research R11); загрузчик бросает `httpx.ReadTimeout` → `ArchiveTimeout`, загрузчик вызван ровно 1 раз (без повтора); загрузчик спит дольше `archive_timeout=0.1` → `ArchiveTimeout` не позже чем через ~0,3 с, задача снята (следующий запрос — новый вызов загрузчика); `httpx.ConnectError` → `ArchiveUnavailable`; статус 500 и 404 → `ArchiveUnavailable`; 200 с телом `<html>oops</html>` → `ArchiveUnavailable`; статус 429 → `ArchiveOverloaded` с `retry_after ≥ 1`; загрузчик бросает `RuntimeError` → `ArchiveUnavailable`, `assertLogs(level="ERROR")` содержит трассировку, а следующий `get_history` другого домена обслуживается (работник жив, plan R-5); каждый вызов пишет в журнал `INFO`/`ERROR` с доменом, итогом и длительностью
- [ ] T020 [P] [US2] Тесты эндпоинта в `tests/test_wayback_api.py` (contracts C6–C9, C12): `ArchiveOverloaded(retry_after=60)` → 503, `code == "archive_overloaded"`, заголовок `Retry-After == "60"`; `ArchiveTimeout` → 504 `archive_timeout`, без `Retry-After`; `ArchiveUnavailable` → 502 `archive_error`; для всех — ключи тела ровно `{"code", "detail"}`, `detail` непустой, в теле нет слов `Traceback`/`Exception`; C12 — для каждого исхода мока (успех 200, «нет снимков» 200, `HistoryPending` 202, `InvalidDomain` 400, 502, 503, 504) и для запроса без параметра заголовок `Content-Type` начинается с `application/json`, а `response.json()` разбирается без ошибки

### Implementation for User Story 2

- [ ] T021 [US2] Реализовать обработку ошибок архива в `WaybackService._execute`/работнике в `wayback.py`: изменить значение по умолчанию `WaybackSettings.archive_timeout` с `10.0` на `30.0` (T002 выполнен со старым значением; FR-021, research R11 — замер 17,5 с в `capture.md`); каждый вызов загрузчика обернуть в `asyncio.wait_for(..., settings.archive_timeout)`; `asyncio.TimeoutError`/`httpx.TimeoutException` → `ArchiveTimeout`; `httpx.RequestError` → `ArchiveUnavailable`; статус 429 → `ArchiveOverloaded(retry_after=<Retry-After архива в секундах, обрезанный до ARCHIVE_RETRY_AFTER_MIN..ARCHIVE_RETRY_AFTER_MAX, иначе int(settings.pause_default)>)` (пауза очереди — US4); прочие не-200 (кроме 403 «исключён») → `ArchiveUnavailable`; работник ловит любое другое исключение `_execute` → `logger.exception` + `ArchiveUnavailable` в `future`, сам не падает; журнал на каждое обращение: домен, итог (`ok`/`empty`/`blocked`/код ошибки), длительность в мс, число строк (research R9); адреса и тексты исключений — только в журнал. Тесты T019 и T020 зелёные

**Checkpoint**: US1 + US2 — любые сбои архива дают различимые ответы по контракту

---

## Phase 5: User Story 3 — Кэш истории на 7 дней (Priority: P3)

**Goal**: повторный запрос в течение 7 дней отдаётся из кэша без обращения к архиву, кэш переживает перезапуск.

**Independent Test**: подсчёт вызовов фейкового загрузчика при повторных запросах; сдвиг `FakeClock` на 7 дней + 1 с; новый экземпляр сервиса с тем же файлом кэша.

### Tests for User Story 3 ⚠️

- [ ] T022 [US3] Тесты кэша в `tests/test_wayback.py`, класс `TestCache` (FR-012–FR-016, SC-002, US3 сценарии 1–5): второй запрос → загрузчик вызван 1 раз, `cached=True`, `checked_at` как у первого; время ответа из кэша, измеренное `time.perf_counter()` вокруг `await get_history(...)`, меньше 0,1 с (SC-002); «0 снимков» кэшируется; после `ArchiveTimeout`/`ArchiveUnavailable`/`ArchiveOverloaded` повторный запрос снова вызывает загрузчик (ошибки не кэшируются); `FakeClock.advance(604800 - 1)` → из кэша, `advance(604800 + 1)` от исходного момента → новый вызов загрузчика; `пример.рф` затем `xn--e1afmkfd.xn--p1ai` → один вызов; новый `WaybackService` с тем же `cache_path` отдаёт запись без вызова загрузчика; устаревшая запись в файле отбрасывается при загрузке; файл с мусором (`"{not json"`) и файл с `"version": 99` → `assertLogs(level="WARNING")`, сервис стартует с пустым кэшем; замоканный `os.replace`, бросающий `OSError` → предупреждение, ответ всё равно успешный; `cache_max_entries=3` и 4 домена → вытеснена запись с самым ранним `checked_at`
- [ ] T023 [P] [US3] Дополнить C11 в `tests/test_wayback_api.py`: настоящий `WaybackService` с фейковым загрузчиком и `cache_path` во временном каталоге через `reset_service(...)`; два запроса `example.ru` через `TestClient(app)` без `with` → оба 200, второй `cached: true`, загрузчик вызван ровно 1 раз

### Implementation for User Story 3

- [ ] T024 [US3] Реализовать кэш в `wayback.py` (research R6, data-model `CacheEntry`): словарь `display → CacheEntry(history, checked_at: float)`; свежесть «`now - checked_at < cache_ttl`»; в `get_history` проверка кэша сразу после нормализации и до постановки в очередь (FR-014), выдача копии с `cached=True`; запись после успеха и «нет снимков» (в кэше `cached=False`), ошибки не пишутся; вытеснение при превышении «`cache_max_entries`» — удалить записи с минимальным `checked_at`; загрузка файла в конструкторе (синхронно, формат `{"version": 1, "entries": {"<display>": {"checked_at": <float>, "history": {...}}}}`, устаревшие и невалидные записи отброшены, `OSError`/`json.JSONDecodeError`/`pydantic.ValidationError` → `logger.warning`); сохранение после каждой новой записи через `await asyncio.to_thread(...)`: запись во временный файл рядом и `os.replace`, создание каталога, `OSError` → `logger.warning`; в `get_service()` путь по умолчанию — `os.environ.get("WAYBACK_CACHE_PATH")` или `<каталог проекта>/.cache/wayback_cache.json`. Тесты T022 и T023 зелёные

**Checkpoint**: US1–US3 — повторные запросы не нагружают архив, кэш сохраняется между перезапусками

---

## Phase 6: User Story 4 — Очередь запросов к архиву с паузами (Priority: P4)

**Goal**: не чаще 1 обращения в секунду, объединение одинаковых доменов, `202 pending` по 15-секундному сроку с фоновой доработкой, 503 только при «429», паузе и полной очереди.

**Independent Test**: 20 одновременных запросов при фейковом загрузчике с задержкой; проверяются моменты вызовов, время ответа каждого запроса, 202 с `retry_after` и последующая выдача из кэша.

### Tests for User Story 4 ⚠️

- [ ] T025 [US4] Тесты очереди в `tests/test_wayback.py`, класс `TestQueue` (FR-017–FR-020, FR-019a, FR-019b, SC-003, SC-003a, SC-006, SC-007, US4 сценарии 1–4, 7, spec Edge Cases): 5 разных доменов одновременно (`asyncio.gather`) → загрузчик никогда не выполняется параллельно, разница между началами вызовов ≥ `request_interval`; 6 одновременных запросов одного домена → 1 вызов, у всех один результат; загрузчик спит дольше `response_deadline` → `HistoryPending` не позже `response_deadline + 0.05`, `retry_after ≥ 1`, затем (после завершения фоновой задачи) `get_history` → `cached=True` без нового вызова; запрос `"XN--E1AFMKFD.XN--P1AI"`, не готовый к сроку → `HistoryPending.domain == "пример.рф"` (U1); нижняя граница ожидания (пауза + интервалы) заранее больше срока (длинная очередь при `request_interval=0.1`, `response_deadline=0.2`) → `HistoryPending` почти сразу (< 0,05 с); запрос, стоящий сразу за выполняемым обращением, при `archive_timeout=0.4` и задержке загрузчика 0,05 с **не** получает 202 сразу, а дожидается результата 200 (решение «202 сразу» не опирается на таймаут обращения, research R5); подсказка `retry_after` при `call_estimate=1.0`: для задач очереди не меньше 1 и не убывает с позицией, для задачи, выполняющейся к сроку, — `max(1, ceil(call_estimate - прошло))`; таймаут обращения больше срока ответа (research R11; `archive_timeout=0.4`, `response_deadline=0.2`): загрузчик отвечает за 0,3 с → первый клиент получает `HistoryPending` к сроку, повтор после завершения → `cached=True` без нового вызова; загрузчик спит 1 с → первый клиент получает `HistoryPending`, повтор через 0,25 с присоединяется к задаче и получает `ArchiveTimeout` в момент таймаута (~0,4 с), задача снята, результат не в кэше, следующий запрос — второй вызов загрузчика; повторный запрос домена, задача которого ещё в очереди, присоединяется к ней (1 вызов); `max_queue=2` и третий новый домен → `ArchiveOverloaded` с `retry_after ≥ 1`, задача не поставлена (загрузчик для него не вызывается); без «429» и при неполной очереди ни один из 20 одновременных запросов не получает `ArchiveOverloaded` (SC-003a); каждый ответ — не позже `response_deadline` (SC-003); SC-006 в масштабе: 20 разных доменов одновременно при `request_interval=0.01` и задержке загрузчика 0,005 с — все 20 оказываются в кэше не позже чем через `20 × (request_interval + 0.005) + 0.5` с от начала (ответы 202 допустимы, после них ждать завершения фоновых задач); отмена клиента: запустить `get_history` как `asyncio.Task`, отменить её во время выполнения загрузчика, дождаться окончания работника → загрузчик вызван 1 раз, результат в кэше (обращение к архиву не отменяется)
- [ ] T026 [US4] Тесты паузы после «429» в `tests/test_wayback.py`, класс `TestPauseAfter429` (FR-022, FR-023, US4 сценарии 5–6): «429» без `Retry-After` → пауза `pause_default`, во время паузы загрузчик не вызывается; запрос нового домена во время паузы → `ArchiveOverloaded` сразу с `retry_after ≥` остатка паузы (округление вверх), задача поставлена в очередь и выполняется после паузы; запрос домена из кэша во время паузы → 200 из кэша; три «429» подряд без `Retry-After` → паузы `0.05`, `0.1`, `0.2`, четвёртая остаётся `0.2` (`pause_max`); успех после «429» сбрасывает удвоение; задача с «429» возвращается в начало очереди один раз, при повторном «429» снимается. `Retry-After` архива главнее (A1). Через сервис, без реального ожидания: «429» с `Retry-After: 120` → `retry_after` следующего запроса нового домена в диапазоне 119–120. Правило длительности — чистой функцией `pause_duration(retry_after: str | None, previous_pause: float | None, settings: WaybackSettings) -> float` (`previous_pause` — длительность паузы, если предыдущий ответ архива тоже был «429», иначе `None`): `("2", 2.0)` → 2 (удвоения нет); `("120", None)` → 120; `(None, 2.0)` с настройками по умолчанию → 4; `(None, None)` → `pause_default`; `(None, 400.0)` по умолчанию → 600; границы `Retry-After` — константы `ARCHIVE_RETRY_AFTER_MIN`/`ARCHIVE_RETRY_AFTER_MAX`, не настройка `pause_max` (U2): `("0", None)` → 1 и `("100000", None)` → 600 при тестовом `pause_max=0.2`; `("Wed, 21 Oct 2026 07:28:00 GMT", None)` → `pause_default`
- [ ] T027 [P] [US4] Тест эндпоинта в `tests/test_wayback_api.py` (contract C3): мок бросает `HistoryPending(domain="example.ru", retry_after=8)` → 202, тело `{"domain": "example.ru", "status": "pending", "retry_after": 8}`, заголовок `Retry-After == "8"`; мок бросает `HistoryPending(domain="пример.рф", retry_after=3)` → в теле `domain == "пример.рф"` (U1)

### Implementation for User Story 4

- [ ] T028 [US4] Реализовать интервал и объединение в `wayback.py`: перед каждым вызовом загрузчика (включая второй вызов `limit=-1` при усечении) работник ждёт `max(last_start + request_interval, pause_until) - monotonic()` и обновляет `last_start`; `jobs` по `display` — повторный запрос присоединяется к существующей задаче (FR-017, FR-018); отмена ожидающего клиента не отменяет задачу. Тесты T025 (интервал, объединение, отмена клиента) зелёные
- [ ] T029 [US4] Реализовать срок ответа и 202 в `WaybackService.get_history` в `wayback.py` (FR-019, FR-019a, research R5, R11): добавить в `WaybackSettings` поле `call_estimate: float = 5.0` (оценка длительности одного обращения); нижняя граница ожидания для позиции `p` = `max(0, pause_until - now) + p × request_interval` — если она > оставшегося срока, сразу `HistoryPending(domain=job.domain.display, retry_after=...)`; подсказка `retry_after` = `max(1, ceil(max(0, pause_until - now) + остаток_текущего + (p + 1) × max(request_interval, call_estimate)))`, где `остаток_текущего = max(0, call_estimate - прошло_с_начала_текущего)` при идущем обращении, иначе 0; для задачи, которая сама выполняется к сроку, — `max(1, ceil(call_estimate - прошло))`; таймаут обращения (`archive_timeout`) в оценках не участвует; иначе `asyncio.wait_for(asyncio.shield(job.future), остаток_срока)`, по таймауту — `HistoryPending(domain=job.domain.display, ...)` с пересчитанной оценкой, задача продолжает выполняться и по завершении пишет результат в кэш; очередь полна (`len(pending) >= max_queue`) и задачи нет → `ArchiveOverloaded(retry_after=max(1, ceil(request_interval)))` без постановки (FR-020). Тесты T025 и T027 зелёные
- [ ] T030 [US4] Реализовать паузу после «429» в `wayback.py` (FR-022, FR-023, FR-019b; research R5). Правило длительности — чистая функция `pause_duration(retry_after, previous_pause, settings)`: если архив прислал `Retry-After` в секундах — длительность = это значение, обрезанное до `ARCHIVE_RETRY_AFTER_MIN..ARCHIVE_RETRY_AFTER_MAX` (1–600 с), удвоение **не применяется**; если заголовка нет или он не в секундах (HTTP-дата не разбирается) — при предыдущем ответе архива «429» `min(last_pause * 2, pause_max)`, иначе `pause_default`; `last_pause` запоминается после каждой паузы и обнуляется первым успехом; `pause_until = monotonic() + длительность`, `logger.warning` с длительностью и источником (`retry-after`/`default`/`doubled`); текущим ожидающим задачи — `ArchiveOverloaded(retry_after=ceil(длительность))`; задача получает новый `future` и встаёт в начало `pending`, если `requeued_after_429` ещё ложно, иначе снимается из `jobs`; в `get_history` при идущей паузе и отсутствии в кэше — поставить/найти задачу (с учётом `max_queue`) и сразу `ArchiveOverloaded(retry_after=ceil(остаток паузы))`. Тесты T026 зелёные

**Checkpoint**: все четыре истории работают; архив защищён от перегрузки

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T031 [P] Проверить отсутствие утечки состояния между тестами (plan R-2): `python -m unittest tests.test_wayback_api tests.test_wayback` и `python -m unittest tests.test_wayback tests.test_wayback_api` дают одинаковый результат; при расхождении — добавить недостающие `reset_service()`/временные каталоги в `tests/test_wayback.py` и `tests/test_wayback_api.py`
- [ ] T032 [P] Пройтись по `wayback.py` на соответствие стилю: аннотации типов у всех публичных функций и классов, docstring на русском, нет `print`, нет `asyncio`-объектов на уровне модуля, нет голого `except:`; длинные функции работника (> ~40 строк) разбить на приватные методы без изменения поведения
- [ ] T033 Запустить `python scripts/check.py` — код выхода 0 и `OK: Все тесты пройдены!`
- [ ] T034 Ручная проверка по `specs/002-wayback-api/quickstart.md`, сценарии M1–M7 (нужна сеть), включая M6 — 20 разных доменов одновременно: в журнале обращения к архиву не чаще 1 в секунду, все 20 попадают в кэш не позже чем через 1 минуту (SC-006 на реальном архиве; если ответы архива медленнее 2 с — записать фактические времена); холодный первый запрос может идти дольше 15 с — тогда ожидается 202 `pending`, а повтор после `Retry-After` — 200 из кэша (research R11); ответы совпадают с ожидаемыми; `git status` не показывает `.cache/wayback_cache.json`; результаты кратко записать в `tests/fixtures/wayback/capture.md` (раздел «Ручная проверка»)
- [ ] T035 Код-ревью изменений скиллом `code-review` с проверкой принципа IV конституции (таймауты, конкретные исключения, журнал, нет 500 и стектрейсов в ответах, кэш с TTL) и соответствия `specs/wayback-contract.md`; исправить найденное в `wayback.py`, `api.py`, `services.py`, `models.py`

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: T001 — первым (образцы нужны тестам разбора T010/T011); T002, T003 — параллельно с T001 (T003 импортирует `wayback` лениво)
- **Foundational (Phase 2)**: после Setup; блокирует все истории
- **US1 (Phase 3)**: после Foundational — MVP
- **US2 (Phase 4)**: после US1 (дополняет `_execute` и эндпоинт US1)
- **US3 (Phase 5)**: после US1; от US2 не зависит по коду, но T022 проверяет «ошибки не кэшируются» на исключениях US2 — удобнее после US2
- **US4 (Phase 6)**: после US1; паузы (T026, T030) опираются на распознавание «429» из US2 (T021), выдача из кэша после 202 — на US3 (T024)
- **Polish (Phase 7)**: после всех историй

### Внутри фазы

- Тесты истории → красные → реализация → зелёные (принцип II)
- `wayback.py` и `tests/test_wayback.py` — общие файлы: задачи, затрагивающие один из них, выполняются последовательно
- T004 (`models.py`) и T005 (`wayback.py`) — параллельно

### Parallel Opportunities

- T002 ‖ T003 ‖ T001 (разные файлы; T001 — ручная задача с сетью)
- T004 ‖ T005
- Тесты эндпоинта (`tests/test_wayback_api.py`: T013, T020, T023, T027) — параллельно с тестами сервиса своей истории (`tests/test_wayback.py`)
- T031 ‖ T032

---

## Parallel Example: User Story 1

```text
# Тесты сервиса в tests/test_wayback.py — последовательно: T009 → T010 → T011 → T012
# Параллельно с ними:
Task: "T013 [P] [US1] Тесты эндпоинта в tests/test_wayback_api.py (C1, C2, C4, C5, C9, C10, C11, C13)"

# Реализация — последовательно в wayback.py, затем эндпоинт:
Task: "T014 normalize_domain" → "T015 parse_cdx" → "T016 make_cdx_fetcher" → "T017 _execute" → "T018 api.py"
```

## Parallel Example: User Story 4

```text
Task: "T025 [US4] Тесты очереди в tests/test_wayback.py" → "T026 [US4] Тесты паузы в tests/test_wayback.py"
Task: "T027 [P] [US4] Тест эндпоинта C3 в tests/test_wayback_api.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1 (T001 — образцы CDX) → Phase 2 (жизненный цикл сервиса)
2. Phase 3 (US1): эндпоинт отдаёт историю, «нет снимков», 400
3. **STOP and VALIDATE**: `python scripts/check.py`, quickstart M1, M4, M5

### Incremental Delivery

1. Setup + Foundational → каркас сервиса
2. US1 → MVP: интерфейс 001-wayback-ui может начинать интеграцию
3. US2 → различимые ошибки (интерфейс показывает правильные сообщения)
4. US3 → кэш 7 дней (повторные открытия мгновенны)
5. US4 → очередь, 202 и паузы (готовность к Топ-20, EXP-005)

Каждая история добавляет поведение, не ломая предыдущие; после каждой — `python scripts/check.py`.

---

## Notes

- [P] = другой файл, нет зависимостей от незавершённых задач
- Все тесты, кроме ручных T001 и T034, работают без сети (принцип II); загрузчик CDX тестируется через `httpx.MockTransport`
- Коммит после каждой задачи или логической группы
- Тесты US1 на усечение (`max_rows=5`) и тесты US4 на интервал используют малые настройки — реальные 100 000 строк и секундные паузы в тестах не нужны
