"""
История домена в веб-архиве (Wayback CDX).

Сервер получает число снимков главной страницы домена, первый и последний снимок.
Все обращения к архиву идут через единую очередь сервиса; объекты asyncio создаются
лениво внутри работающего цикла событий (см. specs/002-wayback-api/research.md, R10).
"""
import asyncio
import datetime
import json
import logging
import math
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping

import httpx

from models import WaybackHistory, WaybackSnapshot

logger = logging.getLogger(__name__)

CDX_URL = "https://web.archive.org/cdx/search/cdx"
SNAPSHOT_URL_PREFIX = "https://web.archive.org/web/"

# Границы Retry-After архива — константы, а не настройки (FR-022)
ARCHIVE_RETRY_AFTER_MIN = 1
ARCHIVE_RETRY_AFTER_MAX = 600


@dataclass(frozen=True)
class WaybackSettings:
    """Настройки очереди, кэша и обращений к архиву (значения по умолчанию — из спецификации)."""

    request_interval: float = 3.0  # 20 обращений в минуту: при 1 с архив блокировал (capture.md)
    response_deadline: float = 15.0
    archive_timeout: float = 30.0
    max_queue: int = 100
    cache_ttl: float = 604800
    cache_max_entries: int = 10000
    max_rows: int = 100000
    call_estimate: float = 5.0  # оценка одного обращения для retry_after (FR-019a)
    pause_default: float = 60
    pause_max: float = 600


@dataclass
class FetchResult:
    """Сырой ответ архива: статус, заголовки и тело без интерпретации."""

    status: int
    headers: Mapping[str, str]
    text: str


# Загрузчик: (punycode-домен, limit) -> ответ архива; limit = -1 — последний снимок
Fetcher = Callable[[str, int], Awaitable[FetchResult]]


@dataclass(frozen=True)
class DomainName:
    """Нормализованное имя домена."""

    display: str  # нижний регистр, без завершающей точки, .рф кириллицей; ключ кэша и задач
    ascii: str  # punycode-форма для запроса к архиву


class WaybackError(Exception):
    """Ошибка получения истории; code — признак для клиента, detail — текст для показа."""

    code = "archive_error"
    status = 502
    default_detail = "Не удалось получить историю из веб-архива"

    def __init__(self, detail: str | None = None, retry_after: int | None = None):
        self.detail = detail or self.default_detail
        self.retry_after = retry_after
        super().__init__(self.detail)


class InvalidDomain(WaybackError):
    code = "invalid_domain"
    status = 400
    default_detail = "Некорректный домен"


class ArchiveOverloaded(WaybackError):
    code = "archive_overloaded"
    status = 503
    default_detail = "Веб-архив перегружен, попробуйте позже"

    def __init__(self, retry_after: int, detail: str | None = None):
        super().__init__(detail, retry_after=retry_after)


class ArchiveTimeout(WaybackError):
    code = "archive_timeout"
    status = 504
    default_detail = "Веб-архив не ответил вовремя"


class ArchiveRateLimited(ArchiveOverloaded):
    """Архив ответил «429»: очередь встаёт на паузу, задача может вернуться в начало очереди."""


class ArchiveUnavailable(WaybackError):
    code = "archive_error"
    status = 502
    default_detail = "Не удалось получить историю из веб-архива"


class HistoryPending(Exception):
    """Задача в очереди, результат не готов к сроку (HTTP 202) — не ошибка."""

    status = 202

    def __init__(self, domain: str, retry_after: int):
        self.domain = domain  # всегда DomainName.display
        self.retry_after = retry_after
        super().__init__(f"{domain}: повторить через {retry_after} с")


ZONES = {"ru", "su", "xn--p1ai"}
_LABEL_RE = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")
_FORBIDDEN_CHARS = set("/:@?#\\")
_TIMESTAMP_FORMAT = "%Y%m%d%H%M%S"
_BLOCKED_MARKERS = ("AdministrativeAccessControlException", "Blocked Site")


def normalize_domain(raw: str) -> DomainName:
    """
    Приводит имя к единой форме и проверяет его (FR-002–FR-004).
    Принимаются только домены второго уровня зон .ru, .su, .рф; иначе InvalidDomain.
    """
    name = raw.strip()
    if name.endswith("."):
        name = name[:-1]
    name = name.lower()
    if not name or any(ch.isspace() or ch in _FORBIDDEN_CHARS for ch in name):
        raise InvalidDomain()
    try:
        ascii_name = name.encode("idna").decode("ascii")
    except UnicodeError:
        raise InvalidDomain()
    labels = ascii_name.split(".")
    if (
        len(ascii_name) > 253
        or len(labels) != 2
        or not all(_LABEL_RE.fullmatch(label) for label in labels)
        or labels[-1] not in ZONES
    ):
        raise InvalidDomain()
    try:
        display = ascii_name.encode("ascii").decode("idna")
    except UnicodeError:
        raise InvalidDomain()
    return DomainName(display=display, ascii=ascii_name)


def _is_timestamp(value: str) -> bool:
    if len(value) != 14 or not (value.isascii() and value.isdigit()):
        return False
    try:
        datetime.datetime.strptime(value, _TIMESTAMP_FORMAT)
    except ValueError:
        return False
    return True


def parse_cdx(text: str) -> list[tuple[str, str]]:
    """
    Разбирает ответ CDX: строки «<14 цифр> <исходный адрес>».
    Битые строки пропускаются; непустой ответ без единой валидной строки — ArchiveUnavailable.
    """
    rows: list[tuple[str, str]] = []
    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) != 2 or not _is_timestamp(parts[0]) or not parts[1].strip():
            skipped += 1
            logger.warning("Пропущена строка CDX с некорректной отметкой времени: %r", line[:100])
            continue
        rows.append((parts[0], parts[1].strip()))
    if skipped and not rows:
        raise ArchiveUnavailable()
    return rows


def make_snapshot(timestamp: str, original: str) -> WaybackSnapshot:
    """Снимок из строки CDX; дата — в UTC, без пересчёта поясов (FR-008, FR-008a)."""
    moment = datetime.datetime.strptime(timestamp, _TIMESTAMP_FORMAT)
    return WaybackSnapshot(
        timestamp=moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        date=moment.strftime("%Y-%m-%d"),
        url=f"{SNAPSHOT_URL_PREFIX}{timestamp}/{original}",
    )


def make_cdx_fetcher(
    timeout: float, transport: httpx.AsyncBaseTransport | None = None
) -> Fetcher:
    """
    Загрузчик CDX по умолчанию. Клиент создаётся на каждый вызов (не привязан к циклу событий);
    таймаут задаётся явно — иначе httpx оборвал бы чтение через свои 5 с (FR-021).
    """

    async def fetch(ascii_domain: str, limit: int) -> FetchResult:
        params = {"url": ascii_domain, "fl": "timestamp,original", "limit": str(limit)}
        if limit == -1:
            params["fastLatest"] = "true"
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.get(CDX_URL, params=params)
            return FetchResult(status=response.status_code, headers=response.headers,
                               text=response.text)

    return fetch


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Заголовок без учёта регистра (httpx.Headers или обычный словарь)."""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _is_seconds(value: str) -> bool:
    # isdigit() пропускает «²» и арабские цифры, на которых int() падает
    return value.isascii() and value.isdigit()


def pause_duration(
    retry_after: str | None, previous_pause: float | None, settings: WaybackSettings
) -> float:
    """
    Длительность паузы очереди после «429» (FR-022).
    Retry-After архива в секундах главнее: берётся как есть (1–600 с), без удвоения.
    Без заголовка (HTTP-дата не разбирается): удвоение предыдущей паузы, если предыдущий
    ответ архива тоже был «429» (previous_pause), иначе pause_default; предел — pause_max.
    """
    value = (retry_after or "").strip()
    if _is_seconds(value):
        return float(min(max(int(value), ARCHIVE_RETRY_AFTER_MIN), ARCHIVE_RETRY_AFTER_MAX))
    if previous_pause is not None:
        return min(previous_pause * 2, settings.pause_max)
    return settings.pause_default


def _utc_iso(unix_time: float) -> str:
    moment = datetime.datetime.fromtimestamp(unix_time, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(eq=False)
class Job:
    """Задача очереди: одна на домен, все ожидающие клиенты получают её результат."""

    domain: DomainName
    future: "asyncio.Future[WaybackHistory]"
    enqueued_at: float
    requeued_after_429: bool = False


CACHE_FILE_VERSION = 1


@dataclass
class CacheEntry:
    """Запись кэша: история (с cached=False) и Unix-время получения из архива."""

    history: WaybackHistory
    checked_at: float


def _write_json_atomic(path: Path, data: dict) -> None:
    """Запись через временный файл и os.replace; ошибка записи не роняет сервис."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Кэш веб-архива не сохранён в %s: %s", path, exc)


def _consume_exception(future: asyncio.Future) -> None:
    # Исключение задачи без ожидающих не должно давать "exception was never retrieved"
    if not future.cancelled():
        future.exception()


def _cancel_worker(worker: "asyncio.Task | None") -> None:
    """Отменяет работника, если его цикл ещё жив; закрытый цикл уже завершил все задачи."""
    if worker is None or worker.done():
        return
    loop = worker.get_loop()
    if loop.is_closed():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        worker.cancel()
    else:
        loop.call_soon_threadsafe(worker.cancel)


class WaybackService:
    """
    Получение истории домена через единую очередь обращений к архиву.

    Конструктор не создаёт объектов asyncio: событие, работник и Future задач создаются
    при первом запросе внутри работающего цикла и пересоздаются при смене цикла.
    """

    def __init__(
        self,
        settings: WaybackSettings,
        fetcher: Fetcher,
        clock: Callable[[], float] = time.time,
        cache_path: Path | None = None,
    ):
        self.settings = settings
        self._fetcher = fetcher
        self._clock = clock
        self._cache_path = cache_path
        self.cache: dict[str, CacheEntry] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.wakeup: asyncio.Event | None = None
        self.worker: asyncio.Task | None = None
        self.last_start = 0.0
        self.pause_until = 0.0
        self.last_pause: float | None = None
        self._reset_runtime()
        self._load_cache()

    def _reset_runtime(self) -> None:
        """
        Сбрасывает очередь и объекты asyncio. Кэш, пауза после «429» и время последнего
        обращения сохраняются: это не asyncio-объекты, а лимиты архива от смены цикла не зависят.
        """
        _cancel_worker(getattr(self, "worker", None))
        self.pending: deque[Job] = deque()
        self.jobs: dict[str, Job] = {}
        self.current: Job | None = None
        self.current_started = 0.0
        self.loop = None
        self.wakeup = None
        self.worker = None

    def _ensure_runtime(self) -> None:
        """Привязывает очередь к текущему циклу событий и запускает работника."""
        loop = asyncio.get_running_loop()
        if self.loop is not loop:
            if self.loop is not None:
                logger.warning("Цикл событий сменился — состояние очереди веб-архива сброшено")
            self._reset_runtime()
            self.loop = loop
            self.wakeup = asyncio.Event()
        if self.worker is None or self.worker.done():
            # Работник умер посреди задачи: её Future никто не завершит — вернуть в начало очереди
            orphans = [job for job in self.jobs.values()
                       if job not in self.pending and not job.future.done()]
            self.pending.extendleft(reversed(orphans))
            self.current = None
            self.worker = loop.create_task(self._worker_loop())

    async def get_history(self, domain: str) -> WaybackHistory:
        """
        История домена не позже response_deadline (FR-019). Не готова к сроку — HistoryPending
        (задача продолжается в фоне); пауза после «429» или полная очередь — ArchiveOverloaded.
        """
        started = time.monotonic()
        # Проверка имени — до очереди: некорректный домен не доходит до архива (FR-003)
        name = normalize_domain(domain)
        # Свежая запись кэша — без очереди и без обращения к архиву, даже во время паузы (FR-014)
        cached = self._cache_get(name.display)
        if cached is not None:
            return cached
        self._ensure_runtime()
        job = self.jobs.get(name.display)
        pause_left = self.pause_until - time.monotonic()
        if pause_left > 0:
            # Пауза после «429»: задачу ставим, клиенту сразу 503 (FR-023)
            if job is None and len(self.pending) < self.settings.max_queue:
                self._enqueue(name)
            raise ArchiveOverloaded(retry_after=max(1, math.ceil(pause_left)))
        if job is None:
            if len(self.pending) >= self.settings.max_queue:
                raise ArchiveOverloaded(
                    retry_after=max(1, math.ceil(self.settings.request_interval)))
            job = self._enqueue(name)
        remaining = self.settings.response_deadline - (time.monotonic() - started)
        if self._lower_bound(job) > remaining:
            raise HistoryPending(name.display, self._retry_hint(job))
        # asyncio.wait не отменяет Future ни по сроку, ни при отмене клиента — задача
        # дорабатывает в фоне; shield здесь не нужен (и в 3.14 шумит в журнале после 202)
        future = job.future
        done, _ = await asyncio.wait({future}, timeout=max(remaining, 0))
        if not done:
            raise HistoryPending(name.display, self._retry_hint(job))
        return future.result()

    def _lower_bound(self, job: Job) -> float:
        """Точно известная часть ожидания: остаток паузы и интервалы перед задачей (R5)."""
        wait = max(0.0, self.pause_until - time.monotonic())
        if job is not self.current and job in self.pending:
            wait += self.pending.index(job) * self.settings.request_interval
        return wait

    def _retry_hint(self, job: Job) -> int:
        """Подсказка retry_after: длительность обращений — по call_estimate, не по таймауту (R5)."""
        now = time.monotonic()
        estimate = self.settings.call_estimate
        pause_left = max(0.0, self.pause_until - now)
        current_left = max(0.0, estimate - (now - self.current_started)) if self.current else 0.0
        if job is self.current:
            return max(1, math.ceil(pause_left + current_left))
        position = self.pending.index(job) if job in self.pending else 0
        step = max(self.settings.request_interval, estimate)
        return max(1, math.ceil(pause_left + current_left + (position + 1) * step))

    def _enqueue(self, name: DomainName) -> Job:
        future = self.loop.create_future()
        future.add_done_callback(_consume_exception)
        job = Job(domain=name, future=future, enqueued_at=time.monotonic())
        self.pending.append(job)
        self.jobs[name.display] = job
        self.wakeup.set()
        return job

    async def _worker_loop(self) -> None:
        while True:
            if not self.pending:
                self.wakeup.clear()
                await self.wakeup.wait()
                continue
            job = self.pending.popleft()
            self.current = job
            self.current_started = time.monotonic()
            try:
                result = await self._execute(job)
            except ArchiveRateLimited as exc:
                logger.info("Wayback %s: %s", job.domain.display, exc.code)
                self._requeue_after_429(job, exc)
            except WaybackError as exc:
                logger.info("Wayback %s: %s", job.domain.display, exc.code)
                self._finish(job, error=exc)
            except Exception:
                # Работник не должен падать: непредвиденная ошибка — archive_error (FR-026)
                logger.exception("Wayback %s: непредвиденная ошибка", job.domain.display)
                self._finish(job, error=ArchiveUnavailable())
            else:
                outcome = "ok" if result.snapshot_count else "empty"
                logger.info("Wayback %s: %s, снимков %d", job.domain.display, outcome,
                            result.snapshot_count)
                try:
                    self._cache_put(job.domain.display, result)
                    await self._save_cache()
                except Exception:
                    # Сбой кэша не должен ронять работника: клиенты всё равно получат результат
                    logger.exception("Кэш веб-архива не обновлён")
                self._finish(job, result=result)
            finally:
                self.current = None

    def _requeue_after_429(self, job: Job, error: ArchiveRateLimited) -> None:
        """Ожидающим — 503; задача один раз возвращается в начало очереди (FR-022)."""
        if job.requeued_after_429:
            self._finish(job, error=error)
            return
        if not job.future.done():
            job.future.set_exception(error)
        job.requeued_after_429 = True
        job.future = self.loop.create_future()
        job.future.add_done_callback(_consume_exception)
        self.pending.appendleft(job)

    def _cache_get(self, key: str) -> WaybackHistory | None:
        entry = self.cache.get(key)
        if entry is None:
            return None
        if self._clock() - entry.checked_at >= self.settings.cache_ttl:
            del self.cache[key]
            return None
        return entry.history.model_copy(update={"cached": True})

    def _cache_put(self, key: str, history: WaybackHistory) -> None:
        """Успех и «нет снимков» кэшируются; ошибки сюда не попадают (FR-012, FR-013)."""
        self.cache[key] = CacheEntry(history.model_copy(update={"cached": False}), self._clock())
        self._evict()

    def _evict(self) -> None:
        """Не более cache_max_entries записей: вытесняются самые старые (FR-016)."""
        while len(self.cache) > self.settings.cache_max_entries:
            oldest = min(self.cache, key=lambda name: self.cache[name].checked_at)
            del self.cache[oldest]

    def _load_cache(self) -> None:
        """Читает файл кэша; битый файл или запись не мешают запуску (FR-015)."""
        if self._cache_path is None or not self._cache_path.exists():
            return
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Кэш веб-архива не прочитан (%s): начинаем с пустого", exc)
            return
        if not isinstance(data, dict) or data.get("version") != CACHE_FILE_VERSION:
            logger.warning("Кэш веб-архива: неизвестный формат файла — начинаем с пустого")
            return
        now = self._clock()
        for key, raw in (data.get("entries") or {}).items():
            try:
                checked_at = float(raw["checked_at"])
                history = WaybackHistory.model_validate(raw["history"])
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Кэш веб-архива: запись %r пропущена (%s)", key, exc)
                continue
            if now - checked_at < self.settings.cache_ttl:
                self.cache[key] = CacheEntry(history, checked_at)
        self._evict()

    async def _save_cache(self) -> None:
        if self._cache_path is None:
            return
        data = {
            "version": CACHE_FILE_VERSION,
            "entries": {
                key: {"checked_at": entry.checked_at, "history": entry.history.model_dump()}
                for key, entry in self.cache.items()
            },
        }
        await asyncio.to_thread(_write_json_atomic, self._cache_path, data)

    def _finish(
        self, job: Job, result: WaybackHistory | None = None, error: Exception | None = None
    ) -> None:
        if self.jobs.get(job.domain.display) is job:
            del self.jobs[job.domain.display]
        if job.future.done():
            return
        if error is not None:
            job.future.set_exception(error)
        else:
            job.future.set_result(result)

    async def _execute(self, job: Job) -> WaybackHistory:
        """Одно обращение к архиву (и второе — за последним снимком при усечении)."""
        name = job.domain
        result = await self._call_archive(name.ascii, self.settings.max_rows)
        if result.status == 403 and any(marker in result.text for marker in _BLOCKED_MARKERS):
            logger.info("Wayback: %s исключён из архива — считаем, что снимков нет", name.display)
            return self._build_history(name, [], None)
        if result.status != 200:
            raise ArchiveUnavailable()
        rows = parse_cdx(result.text)
        last_row = max(rows) if rows else None
        if len(rows) >= self.settings.max_rows:
            tail = await self._call_archive(name.ascii, -1)
            if tail.status != 200:
                raise ArchiveUnavailable()
            tail_rows = parse_cdx(tail.text)
            if tail_rows:
                last_row = max(tail_rows)
        return self._build_history(name, rows, last_row)

    async def _call_archive(self, ascii_domain: str, limit: int) -> FetchResult:
        """
        Одно обращение к архиву: не чаще request_interval и не во время паузы (FR-017, FR-022),
        с общим таймаутом (FR-021); сетевые ошибки и «429» превращаются в WaybackError.
        Адреса и тексты исключений — только в журнал.
        """
        await self._throttle()
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._fetcher(ascii_domain, limit), self.settings.archive_timeout
            )
        except (asyncio.TimeoutError, httpx.TimeoutException):
            self.last_pause = None
            self._log_call(ascii_domain, limit, started, "таймаут", logging.ERROR)
            raise ArchiveTimeout()
        except httpx.RequestError as exc:
            self.last_pause = None
            self._log_call(ascii_domain, limit, started, f"сетевая ошибка {exc!r}", logging.ERROR)
            raise ArchiveUnavailable()
        blocked = result.status == 403 and any(m in result.text for m in _BLOCKED_MARKERS)
        level = logging.INFO if result.status == 200 or blocked else logging.ERROR
        self._log_call(ascii_domain, limit, started, f"HTTP {result.status}", level)
        if result.status == 429:
            raise self._pause_after_429(result.headers)
        self.last_pause = None  # серия «429» прервана
        return result

    async def _throttle(self) -> None:
        delay = max(self.last_start + self.settings.request_interval, self.pause_until)
        delay -= time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self.last_start = self.current_started = time.monotonic()

    def _pause_after_429(self, headers: Mapping[str, str]) -> ArchiveRateLimited:
        retry_after = _header(headers, "Retry-After")
        duration = pause_duration(retry_after, self.last_pause, self.settings)
        if _is_seconds((retry_after or "").strip()):
            source = "retry-after"
        else:
            source = "doubled" if self.last_pause is not None else "default"
        self.last_pause = duration
        self.pause_until = time.monotonic() + duration
        logger.warning("Wayback: архив ответил 429 — пауза очереди %.2f с (%s)", duration, source)
        return ArchiveRateLimited(retry_after=max(1, math.ceil(duration)))

    @staticmethod
    def _log_call(ascii_domain: str, limit: int, started: float, outcome: str, level: int) -> None:
        elapsed_ms = round((time.monotonic() - started) * 1000)
        logger.log(level, "Wayback CDX %s (limit=%s): %s за %d мс",
                   ascii_domain, limit, outcome, elapsed_ms)

    def _build_history(
        self, name: DomainName, rows: list[tuple[str, str]], last_row: tuple[str, str] | None
    ) -> WaybackHistory:
        # 14-значные отметки сравниваются как строки в хронологическом порядке
        first = make_snapshot(*min(rows)) if rows else None
        last = make_snapshot(*last_row) if rows and last_row else None
        return WaybackHistory(
            domain=name.display,
            snapshot_count=len(rows),
            count_truncated=len(rows) >= self.settings.max_rows,
            first=first,
            last=last,
            cached=False,
            checked_at=_utc_iso(self._clock()),
        )


_service: WaybackService | None = None


def _default_cache_path() -> Path:
    """WAYBACK_CACHE_PATH или <каталог проекта>/.cache/wayback_cache.json (вне git)."""
    configured = os.environ.get("WAYBACK_CACHE_PATH")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent / ".cache" / "wayback_cache.json"


def get_service() -> WaybackService:
    """Модульный сервис; создаётся лениво при первом обращении."""
    global _service
    if _service is None:
        settings = WaybackSettings()
        _service = WaybackService(settings, make_cdx_fetcher(settings.archive_timeout),
                                  cache_path=_default_cache_path())
    return _service


def reset_service(service: WaybackService | None = None) -> None:
    """Подменяет или сбрасывает модульный сервис (для тестов); старый работник отменяется."""
    global _service
    if _service is not None and _service is not service:
        _cancel_worker(_service.worker)
    _service = service


async def shutdown_service() -> None:
    """Останавливает работника модульного сервиса (обработчик shutdown приложения)."""
    if _service is None or _service.worker is None or _service.worker.done():
        return
    worker = _service.worker
    _cancel_worker(worker)
    if worker.get_loop() is asyncio.get_running_loop():
        try:
            await worker
        except asyncio.CancelledError:
            pass
