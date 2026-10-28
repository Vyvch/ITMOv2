"""
История домена в веб-архиве (Wayback CDX).

Сервер получает число снимков главной страницы домена, первый и последний снимок.
Все обращения к архиву идут через единую очередь сервиса; объекты asyncio создаются
лениво внутри работающего цикла событий (см. specs/002-wayback-api/research.md, R10).
"""
import asyncio
import datetime
import logging
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

    request_interval: float = 1.0
    response_deadline: float = 15.0
    archive_timeout: float = 10.0
    max_queue: int = 100
    cache_ttl: float = 604800
    cache_max_entries: int = 10000
    max_rows: int = 100000
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
        self.loop: asyncio.AbstractEventLoop | None = None
        self.wakeup: asyncio.Event | None = None
        self.worker: asyncio.Task | None = None
        self._reset_runtime()

    def _reset_runtime(self) -> None:
        """Сбрасывает состояние выполнения (очередь, паузу); кэш не трогает."""
        _cancel_worker(getattr(self, "worker", None))
        self.pending: deque[Job] = deque()
        self.jobs: dict[str, Job] = {}
        self.current: Job | None = None
        self.current_started = 0.0
        self.last_start = 0.0
        self.pause_until = 0.0
        self.last_pause: float | None = None
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
            self.worker = loop.create_task(self._worker_loop())

    async def get_history(self, domain: str) -> WaybackHistory:
        """Возвращает историю домена; ждёт результата задачи очереди."""
        # Проверка имени — до очереди: некорректный домен не доходит до архива (FR-003)
        name = normalize_domain(domain)
        self._ensure_runtime()
        job = self.jobs.get(name.display) or self._enqueue(name)
        # shield: отмена клиента не отменяет задачу, её результат нужен остальным
        return await asyncio.shield(job.future)

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
            except WaybackError as exc:
                self._finish(job, error=exc)
            else:
                self._finish(job, result=result)
            finally:
                self.current = None

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
        return await self._fetcher(ascii_domain, limit)

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


def get_service() -> WaybackService:
    """Модульный сервис; создаётся лениво при первом обращении."""
    global _service
    if _service is None:
        settings = WaybackSettings()
        _service = WaybackService(settings, make_cdx_fetcher(settings.archive_timeout))
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
