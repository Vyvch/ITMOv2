import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx

# Добавляем корневую папку проекта в пути для импорта
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
# Импортируем модули, а не имена: тесты обращаются к wayback.X во время выполнения
import wayback  # noqa: E402
import models  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "wayback"

TEST_SETTINGS = {
    "request_interval": 0.01,
    "response_deadline": 0.2,
    "archive_timeout": 0.1,
    "pause_default": 0.05,
    "pause_max": 0.2,
}


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_settings(**overrides):
    return wayback.WaybackSettings(**{**TEST_SETTINGS, **overrides})


def ok(text: str = "", status: int = 200, headers: dict | None = None):
    """Ответ архива для фейкового загрузчика."""
    return wayback.FetchResult(status=status, headers=headers or {}, text=text)


class Delayed:
    """Ответ фейкового загрузчика с задержкой."""

    def __init__(self, seconds: float, item):
        self.seconds = seconds
        self.item = item


class FakeFetcher:
    """
    Фейковый загрузчик CDX. Сценарий: ключ (домен, limit), домен или "*" -> ответ
    (FetchResult, исключение или Delayed) либо список ответов, выдаваемых по очереди
    (последний повторяется).
    """

    def __init__(self, script: dict | None = None):
        self.script = dict(script or {})
        self.calls: list[tuple[str, int, float]] = []
        self.active = 0
        self.max_active = 0

    def _next(self, ascii_domain: str, limit: int):
        for key in ((ascii_domain, limit), ascii_domain, "*"):
            if key in self.script:
                item = self.script[key]
                if isinstance(item, list):
                    return item.pop(0) if len(item) > 1 else item[0]
                return item
        raise AssertionError(f"Нет сценария для {ascii_domain!r}, limit={limit}")

    def count(self, ascii_domain: str | None = None) -> int:
        return sum(1 for call in self.calls if ascii_domain in (None, call[0]))

    async def __call__(self, ascii_domain: str, limit: int):
        self.calls.append((ascii_domain, limit, time.monotonic()))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            item = self._next(ascii_domain, limit)
            if isinstance(item, Delayed):
                await asyncio.sleep(item.seconds)
                item = item.item
            if isinstance(item, BaseException):
                raise item
            return item
        finally:
            self.active -= 1


class FakeClock:
    """Подменяемые часы (Unix-время) для проверки срока жизни кэша."""

    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_service(tmpdir, fetcher, clock=None, **overrides):
    return wayback.WaybackService(
        make_settings(**overrides),
        fetcher,
        clock=clock or time.time,
        cache_path=Path(tmpdir) / "wayback_cache.json",
    )


def make_stub_service(**overrides):
    """Сервис с готовым ответом вместо разбора CDX — для тестов жизненного цикла."""

    class StubService(wayback.WaybackService):
        async def _execute(self, job):
            return models.WaybackHistory(
                domain=job.domain.display, snapshot_count=0, count_truncated=False,
                first=None, last=None, cached=False, checked_at="2026-10-28T00:00:00Z",
            )

    return StubService(make_settings(**overrides), FakeFetcher({"*": ok()}))


ASYNCIO_TYPES = (asyncio.Future, asyncio.Event, asyncio.Lock, asyncio.Queue, asyncio.Condition)


class TestServiceLifecycle(unittest.TestCase):
    """Ленивое создание asyncio-объектов, смена цикла событий, сброс между тестами (R10)."""

    def tearDown(self):
        wayback.reset_service()

    def test_no_asyncio_objects_at_import_and_construction(self):
        """Ни модуль, ни конструктор сервиса не создают asyncio-объектов."""
        for name, value in vars(wayback).items():
            self.assertNotIsInstance(value, ASYNCIO_TYPES, name)
        service = make_stub_service()
        self.assertIsNone(service.loop)
        self.assertIsNone(service.wakeup)
        self.assertIsNone(service.worker)
        wayback.reset_service()
        self.assertIsNone(wayback.get_service().worker)

    def test_survives_event_loop_change(self):
        """Два asyncio.run подряд на одном сервисе — оба вызова завершаются."""
        service = make_stub_service()

        async def call():
            return await asyncio.wait_for(service.get_history("example.ru"), 2)

        first = asyncio.run(call())
        first_loop = service.loop
        second = asyncio.run(call())
        self.assertEqual(first.domain, "example.ru")
        self.assertEqual(second.domain, "example.ru")
        self.assertIsNot(service.loop, first_loop)

    def test_reset_service(self):
        """reset_service подставляет экземпляр или сбрасывает модульный сервис."""
        import services

        custom = make_stub_service()
        wayback.reset_service(custom)
        self.assertIs(wayback.get_service(), custom)
        services.reset_service()
        fresh = wayback.get_service()
        self.assertIsNot(fresh, custom)
        services.reset_service(custom)
        self.assertIs(wayback.get_service(), custom)

    def test_shutdown_service_stops_worker(self):
        """shutdown_service (и его реэкспорт в services) останавливает работника."""
        import services

        for shutdown in (wayback.shutdown_service, services.shutdown_service):
            with self.subTest(shutdown=shutdown.__module__):
                service = make_stub_service()
                wayback.reset_service(service)

                async def scenario():
                    await service.get_history("example.ru")
                    await shutdown()
                    return service.worker.done()

                self.assertTrue(asyncio.run(scenario()))

    def test_dead_worker_is_restarted(self):
        """Если работник завершился, следующий запрос запускает нового."""
        service = make_stub_service()

        async def scenario():
            await service.get_history("example.ru")
            old_worker = service.worker
            old_worker.cancel()
            await asyncio.sleep(0)
            result = await asyncio.wait_for(service.get_history("other.ru"), 2)
            return old_worker, result

        old_worker, result = asyncio.run(scenario())
        self.assertTrue(old_worker.done())
        self.assertIsNot(service.worker, old_worker)
        self.assertEqual(result.domain, "other.ru")


ISO_Z = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


class TestNormalizeDomain(unittest.TestCase):
    """Нормализация и проверка имени домена (FR-002–FR-004, R3)."""

    def test_latin_variants(self):
        for raw in ["Example.RU", " example.ru ", "example.ru."]:
            with self.subTest(raw=raw):
                name = wayback.normalize_domain(raw)
                self.assertEqual(name.display, "example.ru")
                self.assertEqual(name.ascii, "example.ru")

    def test_cyrillic_and_punycode_give_same_name(self):
        for raw in ["пример.рф", "ПРИМЕР.РФ", "xn--e1afmkfd.xn--p1ai", "XN--E1AFMKFD.XN--P1AI"]:
            with self.subTest(raw=raw):
                name = wayback.normalize_domain(raw)
                self.assertEqual(name.display, "пример.рф")
                self.assertEqual(name.ascii, "xn--e1afmkfd.xn--p1ai")

    def test_su_zone(self):
        self.assertEqual(wayback.normalize_domain("shop.su").display, "shop.su")

    def test_invalid_names(self):
        invalid = [
            "", "   ", "http://example.ru/page", "example.ru/", "example.ru:80",
            "user@example.ru", "exa mple.ru", "example.com", "ru", "a..ru", "-abc.ru",
            "abc-.ru", "a" * 64 + ".ru", "a" * 250 + ".ru", "exa_mple.ru",
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(wayback.InvalidDomain):
                    wayback.normalize_domain(raw)

    def test_subdomains_rejected(self):
        """Только домены второго уровня (FR-003)."""
        subdomains = [
            "www.example.ru", "WWW.Example.RU.", "shop.msk.ru", "a.b.example.ru",
            "www.пример.рф", "www.xn--e1afmkfd.xn--p1ai", "m.shop.su",
        ]
        for raw in subdomains:
            with self.subTest(raw=raw):
                with self.assertRaises(wayback.InvalidDomain):
                    wayback.normalize_domain(raw)


class TestParseCdx(unittest.TestCase):
    """Разбор ответа CDX (FR-006, FR-008, FR-008a, FR-010, FR-011)."""

    def test_regular_fixture(self):
        rows = wayback.parse_cdx(load_fixture("cdx_regular.txt"))
        self.assertEqual(len(rows), 194)
        self.assertEqual(rows[0], ("20010330041232", "http://www.example.ru:80/"))
        self.assertEqual(rows[-1], ("20230510085431", "http://example.ru/"))

    def test_snapshot_fields(self):
        snapshot = wayback.make_snapshot("20091231233000", "http://example.ru/")
        self.assertEqual(snapshot.timestamp, "2009-12-31T23:30:00Z")
        self.assertEqual(snapshot.date, "2009-12-31")
        self.assertEqual(snapshot.url, "https://web.archive.org/web/20091231233000/http://example.ru/")

    def test_empty(self):
        self.assertEqual(wayback.parse_cdx(load_fixture("cdx_empty.txt")), [])
        self.assertEqual(wayback.parse_cdx(""), [])

    def test_bad_lines_skipped(self):
        text = ("2009 http://a.ru/\nabcdefghijklmn http://a.ru/\n20091331000000 http://a.ru/\n"
                "20100101000000 http://a.ru/\n")
        with self.assertLogs("wayback", level="WARNING"):
            rows = wayback.parse_cdx(text)
        self.assertEqual(rows, [("20100101000000", "http://a.ru/")])

    def test_only_bad_lines(self):
        with self.assertLogs("wayback", level="WARNING"):
            with self.assertRaises(wayback.ArchiveUnavailable):
                wayback.parse_cdx("<html>oops</html>\n2009 x\n")


class TestGetHistoryUS1(unittest.IsolatedAsyncioTestCase):
    """История домена через сервис с фейковым загрузчиком (US1)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    async def test_regular_history(self):
        fetcher = FakeFetcher({"example.ru": ok(load_fixture("cdx_regular.txt"))})
        history = await make_service(self.tmp.name, fetcher).get_history("example.ru")
        self.assertEqual(history.domain, "example.ru")
        self.assertEqual(history.snapshot_count, 194)
        self.assertFalse(history.count_truncated)
        self.assertFalse(history.cached)
        self.assertRegex(history.checked_at, ISO_Z)
        self.assertEqual(history.first.date, "2001-03-30")
        self.assertEqual(history.first.url,
                         "https://web.archive.org/web/20010330041232/http://www.example.ru:80/")
        self.assertEqual(history.last.timestamp, "2023-05-10T08:54:31Z")

    async def test_cyrillic_domain_uses_punycode(self):
        fetcher = FakeFetcher({"*": ok("20200101000000 http://xn--e1afmkfd.xn--p1ai/\n")})
        service = make_service(self.tmp.name, fetcher)
        history = await service.get_history("пример.рф")
        self.assertEqual(fetcher.calls[0][:2], ("xn--e1afmkfd.xn--p1ai", service.settings.max_rows))
        self.assertEqual(history.domain, "пример.рф")
        self.assertEqual(history.snapshot_count, 1)
        self.assertEqual(history.first, history.last)

    async def test_no_snapshots(self):
        fetcher = FakeFetcher({"*": ok(load_fixture("cdx_empty.txt"))})
        history = await make_service(self.tmp.name, fetcher).get_history("zq7x9k2m4p8w.ru")
        self.assertEqual(history.snapshot_count, 0)
        self.assertIsNone(history.first)
        self.assertIsNone(history.last)

    async def test_invalid_domain_does_not_call_archive(self):
        fetcher = FakeFetcher({"*": ok()})
        service = make_service(self.tmp.name, fetcher)
        for raw in ["example.com", "www.example.ru", ""]:
            with self.subTest(raw=raw):
                with self.assertRaises(wayback.InvalidDomain):
                    await service.get_history(raw)
        self.assertEqual(fetcher.count(), 0)

    async def test_blocked_site_is_no_snapshots(self):
        blocked = ("org.archive.wayback.exception.AdministrativeAccessControlException: "
                   "Blocked Site Error")
        fetcher = FakeFetcher({"*": ok(blocked, status=403)})
        with self.assertLogs("wayback", level="INFO") as logs:
            history = await make_service(self.tmp.name, fetcher).get_history("blocked.ru")
        self.assertEqual(history.snapshot_count, 0)
        self.assertTrue(any("исключ" in line for line in logs.output))

    async def test_truncated_count_fetches_last(self):
        rows = "".join(f"2010010100000{i} http://big.ru/\n" for i in range(5))
        fetcher = FakeFetcher({
            ("big.ru", 5): ok(rows),
            ("big.ru", -1): ok("20251231235959 http://big.ru/\n"),
        })
        history = await make_service(self.tmp.name, fetcher, max_rows=5).get_history("big.ru")
        self.assertTrue(history.count_truncated)
        self.assertEqual(history.snapshot_count, 5)
        self.assertEqual([call[1] for call in fetcher.calls], [5, -1])
        self.assertEqual(history.last.timestamp, "2025-12-31T23:59:59Z")

    async def test_truncated_last_failure_fails_request(self):
        rows = "".join(f"2010010100000{i} http://big.ru/\n" for i in range(5))
        fetcher = FakeFetcher({("big.ru", 5): ok(rows), ("big.ru", -1): ok("", status=500)})
        with self.assertRaises(wayback.ArchiveUnavailable):
            await make_service(self.tmp.name, fetcher, max_rows=5).get_history("big.ru")


class TestCdxFetcher(unittest.IsolatedAsyncioTestCase):
    """Загрузчик CDX через httpx.MockTransport, без сети (FR-005–FR-007, FR-021)."""

    def make(self, responder, timeout=7.5):
        self.requests = []

        def handler(request):
            self.requests.append(request)
            return responder(request)

        return wayback.make_cdx_fetcher(timeout, transport=httpx.MockTransport(handler))

    async def test_request_params(self):
        fetcher = self.make(lambda request: httpx.Response(200, text=""))
        await fetcher("xn--e1afmkfd.xn--p1ai", 100000)
        request = self.requests[0]
        self.assertEqual(str(request.url.copy_with(query=None)), wayback.CDX_URL)
        self.assertEqual(dict(request.url.params), {
            "url": "xn--e1afmkfd.xn--p1ai", "fl": "timestamp,original", "limit": "100000",
        })

    async def test_latest_params(self):
        fetcher = self.make(lambda request: httpx.Response(200, text=""))
        await fetcher("example.ru", -1)
        params = dict(self.requests[0].url.params)
        self.assertEqual(params["limit"], "-1")
        self.assertEqual(params["fastLatest"], "true")

    async def test_explicit_timeout(self):
        """Таймаут клиента — переданный, а не 5 с httpx по умолчанию."""
        fetcher = self.make(lambda request: httpx.Response(200, text=""), timeout=7.5)
        await fetcher("example.ru", 100000)
        timeout = self.requests[0].extensions["timeout"]
        self.assertEqual(timeout["read"], 7.5)
        self.assertEqual(timeout["connect"], 7.5)

    async def test_responses_passed_through(self):
        cases = [
            httpx.Response(200, text=load_fixture("cdx_regular.txt")),
            httpx.Response(429, headers={"Retry-After": "30"}, text="slow down"),
            httpx.Response(403, text="Blocked Site Error"),
        ]
        for response in cases:
            with self.subTest(status=response.status_code):
                fetcher = self.make(lambda request, response=response: response)
                result = await fetcher("example.ru", 100000)
                self.assertEqual(result.status, response.status_code)
                self.assertEqual(result.text, response.text)
                self.assertEqual(result.headers.get("retry-after"),
                                 response.headers.get("retry-after"))

    async def test_network_error_propagates(self):
        def fail(request):
            raise httpx.ConnectError("нет связи", request=request)

        with self.assertRaises(httpx.ConnectError):
            await self.make(fail)("example.ru", 100000)
