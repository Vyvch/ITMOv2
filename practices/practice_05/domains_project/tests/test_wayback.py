import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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

        async def call(domain):
            return await asyncio.wait_for(service.get_history(domain), 2)

        first = asyncio.run(call("example.ru"))
        first_loop = service.loop
        # другой домен: повтор того же отдался бы из кэша, не затрагивая цикл (US3)
        second = asyncio.run(call("other.ru"))
        self.assertEqual(first.domain, "example.ru")
        self.assertEqual(second.domain, "other.ru")
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


class TestArchiveErrors(unittest.IsolatedAsyncioTestCase):
    """Сбои архива дают различимые ошибки, журнал и живого работника (US2)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def service(self, script, **overrides):
        self.fetcher = FakeFetcher(script)
        return make_service(self.tmp.name, self.fetcher, **overrides)

    def test_default_archive_timeout_is_30_seconds(self):
        """FR-021, research R11: замер 17,5 с на холодном архиве."""
        self.assertEqual(wayback.WaybackSettings().archive_timeout, 30.0)

    async def test_httpx_timeout(self):
        service = self.service({"*": httpx.ReadTimeout("медленно")})
        with self.assertRaises(wayback.ArchiveTimeout):
            await service.get_history("slow.ru")
        self.assertEqual(self.fetcher.count(), 1)

    async def test_total_timeout_and_job_removed(self):
        service = self.service({"*": Delayed(1.0, ok())}, archive_timeout=0.1)
        started = time.monotonic()
        with self.assertRaises(wayback.ArchiveTimeout):
            await service.get_history("slow.ru")
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertNotIn("slow.ru", service.jobs)
        with self.assertRaises(wayback.ArchiveTimeout):
            await service.get_history("slow.ru")
        self.assertEqual(self.fetcher.count(), 2)

    async def test_network_and_status_errors(self):
        cases = {
            "network": httpx.ConnectError("нет связи"),
            "server": ok("", status=500),
            "missing": ok("", status=404),
            "garbage": ok("<html>oops</html>"),
        }
        for label, answer in cases.items():
            with self.subTest(label=label):
                service = self.service({"*": answer})
                with self.assertLogs("wayback", level="INFO"):
                    with self.assertRaises(wayback.ArchiveUnavailable):
                        await service.get_history("broken.ru")

    async def test_rate_limited(self):
        service = self.service({"*": ok("slow down", status=429, headers={"Retry-After": "30"})})
        with self.assertRaises(wayback.ArchiveOverloaded) as ctx:
            await service.get_history("busy.ru")
        self.assertEqual(ctx.exception.retry_after, 30)
        service = self.service({"*": ok("slow down", status=429)})
        with self.assertRaises(wayback.ArchiveOverloaded) as ctx:
            await service.get_history("busy.ru")
        self.assertGreaterEqual(ctx.exception.retry_after, 1)

    async def test_unexpected_error_keeps_worker_alive(self):
        """Непредвиденная ошибка — archive_error с трассировкой в журнале, работник жив (R-5)."""
        service = self.service({"bug.ru": RuntimeError("неожиданно"), "fine.ru": ok("")})
        with self.assertLogs("wayback", level="ERROR") as logs:
            with self.assertRaises(wayback.ArchiveUnavailable):
                await asyncio.wait_for(service.get_history("bug.ru"), 2)
        self.assertTrue(any("Traceback" in line for line in logs.output))
        history = await asyncio.wait_for(service.get_history("fine.ru"), 2)
        self.assertEqual(history.snapshot_count, 0)

    async def test_each_call_is_logged(self):
        service = self.service({"*": ok(load_fixture("cdx_regular.txt"))})
        with self.assertLogs("wayback", level="INFO") as logs:
            await service.get_history("example.ru")
        line = next(line for line in logs.output if "example.ru" in line)
        self.assertRegex(line, r"\d+ мс")
        self.assertIn("194", " ".join(logs.output))


WEEK = 604800


class TestCache(unittest.IsolatedAsyncioTestCase):
    """Кэш истории на 7 дней с сохранением на диск (US3, FR-012–FR-016)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.cache_path = Path(self.tmp.name) / "wayback_cache.json"

    def tearDown(self):
        self.tmp.cleanup()

    def service(self, script=None, **overrides):
        self.fetcher = FakeFetcher(script or {"*": ok(load_fixture("cdx_regular.txt"))})
        return make_service(self.tmp.name, self.fetcher, clock=self.clock, **overrides)

    async def test_second_request_from_cache(self):
        service = self.service()
        first = await service.get_history("example.ru")
        started = time.perf_counter()
        second = await service.get_history("example.ru")
        self.assertLess(time.perf_counter() - started, 0.1)  # SC-002
        self.assertEqual(self.fetcher.count(), 1)
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(second.checked_at, first.checked_at)
        self.assertEqual(second.snapshot_count, first.snapshot_count)

    async def test_no_snapshots_cached(self):
        service = self.service({"*": ok("")})
        await service.get_history("zq7x9k2m4p8w.ru")
        self.assertTrue((await service.get_history("zq7x9k2m4p8w.ru")).cached)
        self.assertEqual(self.fetcher.count(), 1)

    async def test_errors_not_cached(self):
        answers = {
            "timeout": httpx.ReadTimeout("медленно"),
            "error": ok("", status=500),
            "overloaded": ok("", status=429, headers={"Retry-After": "1"}),
        }
        for label, answer in answers.items():
            with self.subTest(label=label):
                self.cache_path.unlink(missing_ok=True)  # у каждого случая свой пустой кэш
                service = self.service({"*": [answer, ok("")]}, pause_default=0.01)
                with self.assertRaises(wayback.WaybackError):
                    await service.get_history("flaky.ru")
                self.assertNotIn("flaky.ru", service.cache)
                if label != "overloaded":  # после «429» задача дорабатывает сама (US4)
                    history = await asyncio.wait_for(service.get_history("flaky.ru"), 2)
                    self.assertFalse(history.cached)
                    self.assertEqual(self.fetcher.count(), 2)

    async def test_ttl_seven_days(self):
        service = self.service()
        await service.get_history("example.ru")
        self.clock.advance(WEEK - 1)
        self.assertTrue((await service.get_history("example.ru")).cached)
        self.clock.advance(2)
        self.assertFalse((await service.get_history("example.ru")).cached)
        self.assertEqual(self.fetcher.count(), 2)

    async def test_cyrillic_and_punycode_share_entry(self):
        service = self.service({"*": ok("20200101000000 http://xn--e1afmkfd.xn--p1ai/\n")})
        await service.get_history("пример.рф")
        history = await service.get_history("xn--e1afmkfd.xn--p1ai")
        self.assertTrue(history.cached)
        self.assertEqual(history.domain, "пример.рф")
        self.assertEqual(self.fetcher.count(), 1)

    async def test_survives_restart(self):
        await self.service().get_history("example.ru")
        self.assertTrue(self.cache_path.exists())
        restarted = self.service()
        history = await restarted.get_history("example.ru")
        self.assertTrue(history.cached)
        self.assertEqual(self.fetcher.count(), 0)

    async def test_stale_entries_dropped_on_load(self):
        await self.service().get_history("example.ru")
        self.clock.advance(WEEK + 1)
        restarted = self.service()
        self.assertFalse((await restarted.get_history("example.ru")).cached)
        self.assertEqual(self.fetcher.count(), 1)

    def test_broken_file_starts_empty(self):
        for content in ["{not json", '{"version": 99, "entries": {}}',
                        '{"version": 1, "entries": {"example.ru": {"checked_at": "x"}}}']:
            with self.subTest(content=content):
                self.cache_path.write_text(content, encoding="utf-8")
                with self.assertLogs("wayback", level="WARNING"):
                    service = self.service()
                self.assertEqual(len(service.cache), 0)

    async def test_write_error_is_not_fatal(self):
        service = self.service()
        with patch("wayback.os.replace", side_effect=OSError("диск недоступен")):
            with self.assertLogs("wayback", level="WARNING"):
                history = await service.get_history("example.ru")
        self.assertEqual(history.snapshot_count, 194)

    async def test_eviction(self):
        service = self.service({"*": ok("")}, cache_max_entries=3)
        for name in ["a.ru", "b.ru", "c.ru", "d.ru"]:
            await service.get_history(name)
            self.clock.advance(10)
        self.assertEqual(set(service.cache), {"b.ru", "c.ru", "d.ru"})


EPS = 0.003  # допуск таймеров


async def timed(coro):
    """(результат или исключение, длительность в секундах)."""
    started = time.monotonic()
    try:
        result = await coro
    except Exception as exc:  # noqa: BLE001 — тесту нужен сам объект исключения
        result = exc
    return result, time.monotonic() - started


async def wait_until(condition, timeout: float = 2.0) -> float:
    """Ждёт выполнения условия; возвращает затраченное время или падает по таймауту."""
    started = time.monotonic()
    while not condition():
        if time.monotonic() - started > timeout:
            raise AssertionError("условие не выполнилось за отведённое время")
        await asyncio.sleep(0.005)
    return time.monotonic() - started


class QueueTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def service(self, script, **overrides):
        self.fetcher = FakeFetcher(script)
        return make_service(self.tmp.name, self.fetcher, **overrides)

    def starts(self, domain: str | None = None) -> list[float]:
        return [call[2] for call in self.fetcher.calls if domain in (None, call[0])]


class TestQueue(QueueTestCase):
    """Очередь, интервал, объединение запросов, срок ответа и 202 (US4)."""

    def test_default_interval_is_3_seconds(self):
        """FR-017: живая проверка M6 — при 1 с архив блокировал соединения (capture.md)."""
        self.assertEqual(wayback.WaybackSettings().request_interval, 3.0)

    async def test_one_call_at_a_time_with_interval(self):
        service = self.service({"*": ok("")})
        await asyncio.gather(*(service.get_history(f"d{i}.ru") for i in range(5)))
        self.assertEqual(self.fetcher.max_active, 1)
        starts = self.starts()
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertTrue(all(gap >= service.settings.request_interval - EPS for gap in gaps), gaps)

    async def test_same_domain_coalesced(self):
        service = self.service({"*": Delayed(0.05, ok(load_fixture("cdx_regular.txt")))})
        results = await asyncio.gather(*(service.get_history("example.ru") for _ in range(6)))
        self.assertEqual(self.fetcher.count(), 1)
        self.assertTrue(all(result.snapshot_count == 194 for result in results))

    async def test_pending_after_deadline_then_cache(self):
        """Обращение дольше срока: 202, доработка в фоне, затем ответ из кэша (R11)."""
        service = self.service({"*": Delayed(0.3, ok(""))}, archive_timeout=0.4)
        result, elapsed = await timed(service.get_history("slow.ru"))
        self.assertIsInstance(result, wayback.HistoryPending)
        self.assertLessEqual(elapsed, service.settings.response_deadline + 0.05)
        self.assertGreaterEqual(result.retry_after, 1)
        await wait_until(lambda: "slow.ru" in service.cache)
        self.assertTrue((await service.get_history("slow.ru")).cached)
        self.assertEqual(self.fetcher.count(), 1)

    async def test_pending_domain_is_display_form(self):
        service = self.service({"*": Delayed(0.3, ok(""))}, archive_timeout=0.4)
        result, _ = await timed(service.get_history("XN--E1AFMKFD.XN--P1AI"))
        self.assertIsInstance(result, wayback.HistoryPending)
        self.assertEqual(result.domain, "пример.рф")

    async def test_immediate_pending_when_queue_is_long(self):
        service = self.service({"*": ok("")}, request_interval=0.1)
        results = await asyncio.gather(*(timed(service.get_history(f"q{i}.ru")) for i in range(5)))
        immediate = [r for r, elapsed in results
                     if isinstance(r, wayback.HistoryPending) and elapsed < 0.05]
        self.assertTrue(immediate, results)

    async def test_waits_behind_running_call(self):
        """Решение «202 сразу» не опирается на таймаут обращения (R5)."""
        service = self.service({"*": Delayed(0.05, ok(""))}, archive_timeout=0.4)
        first, second = await asyncio.gather(service.get_history("a.ru"),
                                              service.get_history("b.ru"))
        self.assertEqual(second.domain, "b.ru")

    async def test_retry_hint(self):
        service = self.service({"*": Delayed(0.3, ok(""))}, archive_timeout=0.4, call_estimate=1.0)
        results = await asyncio.gather(*(service.get_history(f"h{i}.ru") for i in range(4)),
                                       return_exceptions=True)
        self.assertTrue(all(isinstance(r, wayback.HistoryPending) for r in results), results)
        hints = [r.retry_after for r in results]
        self.assertEqual(hints[0], 1)  # выполняется к сроку: ceil(1.0 - 0.2)
        self.assertEqual(hints, sorted(hints))
        self.assertGreater(hints[-1], hints[0])

    async def test_repeat_joins_existing_job(self):
        service = self.service({"*": Delayed(0.3, ok(""))}, archive_timeout=0.4)
        first, _ = await timed(service.get_history("slow.ru"))
        self.assertIsInstance(first, wayback.HistoryPending)
        second = await service.get_history("slow.ru")
        self.assertEqual(second.domain, "slow.ru")
        self.assertEqual(self.fetcher.count(), 1)

    async def test_queue_full(self):
        service = self.service({"*": Delayed(0.3, ok(""))}, archive_timeout=0.4, max_queue=2)
        # a.ru — выполняется, b.ru и c.ru заполняют очередь из двух мест
        tasks = [asyncio.create_task(service.get_history("a.ru"))]
        await wait_until(lambda: service.current is not None)
        tasks += [asyncio.create_task(service.get_history(name)) for name in ["b.ru", "c.ru"]]
        await wait_until(lambda: len(service.pending) == 2)
        result, elapsed = await timed(service.get_history("d.ru"))
        self.assertIsInstance(result, wayback.ArchiveOverloaded)
        self.assertGreaterEqual(result.retry_after, 1)
        self.assertLess(elapsed, 0.05)
        self.assertNotIn("d.ru", service.jobs)
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertNotIn("d.ru", [call[0] for call in self.fetcher.calls])

    async def test_twenty_domains(self):
        """SC-003, SC-003a, SC-006 в масштабе: без 503, ответы в срок, все 20 — в кэше."""
        service = self.service({"*": Delayed(0.005, ok(""))})
        started = time.monotonic()
        results = await asyncio.gather(*(timed(service.get_history(f"t{i}.ru")) for i in range(20)))
        for result, elapsed in results:
            self.assertNotIsInstance(result, wayback.ArchiveOverloaded)
            self.assertLessEqual(elapsed, service.settings.response_deadline + 0.05)
        await wait_until(lambda: len(service.cache) == 20)
        self.assertLessEqual(time.monotonic() - started, 20 * (0.01 + 0.005) + 0.5)

    async def test_client_cancel_does_not_cancel_call(self):
        service = self.service({"*": Delayed(0.1, ok(""))}, archive_timeout=0.4)
        task = asyncio.create_task(service.get_history("slow.ru"))
        await wait_until(lambda: self.fetcher.count() == 1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await wait_until(lambda: "slow.ru" in service.cache)
        self.assertEqual(self.fetcher.count(), 1)

    async def test_archive_timeout_after_pending(self):
        """Таймаут обращения больше срока: 202, затем 504 ожидающему, задача снята (R11)."""
        service = self.service({"*": Delayed(1.0, ok(""))}, archive_timeout=0.4)
        first, _ = await timed(service.get_history("hang.ru"))
        self.assertIsInstance(first, wayback.HistoryPending)
        await asyncio.sleep(0.05)
        second, _ = await timed(service.get_history("hang.ru"))
        self.assertIsInstance(second, wayback.ArchiveTimeout)
        self.assertNotIn("hang.ru", service.cache)
        self.assertNotIn("hang.ru", service.jobs)
        third, _ = await timed(service.get_history("hang.ru"))
        self.assertIsInstance(third, wayback.HistoryPending)
        self.assertEqual(self.fetcher.count(), 2)


class TestWorkerRobustness(QueueTestCase):
    """Задача не «зависает», если работник умер посреди неё или упало сохранение кэша (R-5)."""

    async def test_orphan_job_requeued_after_worker_restart(self):
        service = self.service({"a.ru": [Delayed(0.3, ok("")), ok("")]}, archive_timeout=0.4)
        first = asyncio.create_task(service.get_history("a.ru"))
        await wait_until(lambda: self.fetcher.count() == 1)
        service.worker.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await wait_until(lambda: service.worker.done())
        history = await service.get_history("a.ru")
        self.assertEqual(history.domain, "a.ru")
        self.assertEqual(self.fetcher.count(), 2)

    async def test_background_failure_after_pending_is_quiet(self):
        """Ошибка задачи после 202 не даёт трассировки «exception in shielded future» (живой прогон)."""
        loop_errors = []
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: loop_errors.append(context))
        service = self.service({"*": Delayed(0.3, ok("", status=500))}, archive_timeout=0.4)
        result, _ = await timed(service.get_history("fail.ru"))
        self.assertIsInstance(result, wayback.HistoryPending)
        await wait_until(lambda: "fail.ru" not in service.jobs)
        await asyncio.sleep(0.01)
        self.assertEqual(loop_errors, [])

    async def test_cache_save_failure_keeps_worker_alive(self):
        service = self.service({"*": ok("")})
        with patch("wayback._write_json_atomic", side_effect=RuntimeError("сбой записи")):
            with self.assertLogs("wayback", level="ERROR"):
                history = await asyncio.wait_for(service.get_history("a.ru"), 2)
        self.assertEqual(history.snapshot_count, 0)
        self.assertFalse(service.worker.done())
        self.assertEqual((await service.get_history("b.ru")).domain, "b.ru")


class TestReviewFixes(QueueTestCase):
    """Регрессии по код-ревью (T035)."""

    def test_unicode_digits_in_retry_after(self):
        defaults = wayback.WaybackSettings()
        self.assertEqual(wayback.pause_duration("٣", None, defaults), defaults.pause_default)
        self.assertEqual(wayback.pause_duration("²", None, defaults), defaults.pause_default)

    async def test_cache_put_failure_keeps_worker_alive(self):
        service = self.service({"*": ok("")})
        with patch.object(service, "_cache_put", side_effect=RuntimeError("сбой")):
            with self.assertLogs("wayback", level="ERROR"):
                history = await asyncio.wait_for(service.get_history("a.ru"), 2)
        self.assertEqual(history.domain, "a.ru")
        self.assertEqual((await service.get_history("b.ru")).domain, "b.ru")

    async def test_load_respects_max_entries(self):
        service = self.service({"*": ok("")})
        for name in ["a.ru", "b.ru", "c.ru"]:
            await service.get_history(name)
        reloaded = self.service({"*": ok("")}, cache_max_entries=2)
        self.assertEqual(set(reloaded.cache), {"b.ru", "c.ru"})

    def test_loop_change_keeps_pause(self):
        service = self.service({"*": ok("")})
        service.pause_until = time.monotonic() + 100
        service.last_start = time.monotonic()
        service._reset_runtime()
        self.assertGreater(service.pause_until, time.monotonic())
        self.assertGreater(service.last_start, 0)

    async def test_unexpected_403_logged_as_error(self):
        service = self.service({"*": ok("Forbidden", status=403)})
        with self.assertLogs("wayback", level="ERROR"):
            with self.assertRaises(wayback.ArchiveUnavailable):
                await service.get_history("a.ru")


class TestPauseDuration(unittest.TestCase):
    """Правило длительности паузы после «429» (FR-022, A1, U2)."""

    def test_rule(self):
        defaults = wayback.WaybackSettings()
        small = make_settings()
        cases = [
            (("2", 2.0, defaults), 2),        # Retry-After главнее, удвоения нет
            (("120", None, defaults), 120),
            ((None, 2.0, defaults), 4),       # без заголовка — удвоение
            ((None, None, defaults), defaults.pause_default),
            ((None, 400.0, defaults), 600),   # предел pause_max
            (("0", None, small), 1),          # границы — константы, не pause_max
            (("100000", None, small), 600),
            (("Wed, 21 Oct 2026 07:28:00 GMT", None, defaults), defaults.pause_default),
        ]
        for args, expected in cases:
            with self.subTest(args=args[:2]):
                self.assertEqual(wayback.pause_duration(*args), expected)


class TestPauseAfter429(QueueTestCase):
    """Пауза очереди после «429» (FR-022, FR-023)."""

    async def test_no_calls_during_pause_and_requeue(self):
        service = self.service({"a.ru": [ok("", status=429), ok("")]})
        with self.assertRaises(wayback.ArchiveOverloaded):
            await service.get_history("a.ru")
        await wait_until(lambda: "a.ru" in service.cache)
        first, second = self.starts("a.ru")
        self.assertGreaterEqual(second - first, service.settings.pause_default - EPS)

    async def test_new_domain_during_pause(self):
        service = self.service({"a.ru": [ok("", status=429), ok("")], "b.ru": ok("")},
                               pause_default=0.1)
        with self.assertRaises(wayback.ArchiveOverloaded):
            await service.get_history("a.ru")
        result, elapsed = await timed(service.get_history("b.ru"))
        self.assertIsInstance(result, wayback.ArchiveOverloaded)
        self.assertGreaterEqual(result.retry_after, 1)
        self.assertLess(elapsed, 0.05)
        self.assertIn("b.ru", service.jobs)
        await wait_until(lambda: "b.ru" in service.cache)
        self.assertGreaterEqual(self.starts("b.ru")[0] - self.starts("a.ru")[0], 0.1 - EPS)

    async def test_cache_served_during_pause(self):
        service = self.service({"c.ru": ok(""), "a.ru": ok("", status=429)}, pause_default=0.5)
        await service.get_history("c.ru")
        with self.assertRaises(wayback.ArchiveOverloaded):
            await service.get_history("a.ru")
        self.assertTrue((await service.get_history("c.ru")).cached)

    async def test_doubling_and_single_requeue(self):
        service = self.service({"*": ok("", status=429)})
        with self.assertRaises(wayback.ArchiveOverloaded):
            await service.get_history("a.ru")
        await wait_until(lambda: "a.ru" not in service.jobs)
        self.assertEqual(self.fetcher.count("a.ru"), 2)  # вернулась в очередь один раз
        with self.assertRaises(wayback.ArchiveOverloaded):
            await service.get_history("b.ru")
        await wait_until(lambda: "b.ru" not in service.jobs and self.fetcher.count() == 4)
        starts = self.starts()
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        for gap, pause in zip(gaps, [0.05, 0.1, 0.2]):
            self.assertGreaterEqual(gap, pause - EPS, gaps)

    async def test_success_resets_doubling(self):
        service = self.service({"a.ru": [ok("", status=429), ok("")],
                                "b.ru": [ok("", status=429), ok("")]})
        with self.assertRaises(wayback.ArchiveOverloaded):
            await service.get_history("a.ru")
        await wait_until(lambda: "a.ru" in service.cache)
        with self.assertRaises(wayback.ArchiveOverloaded):
            await service.get_history("b.ru")
        self.assertEqual(service.last_pause, service.settings.pause_default)

    async def test_archive_retry_after_wins(self):
        self.fetcher = FakeFetcher({"a.ru": ok("", status=429, headers={"Retry-After": "120"}),
                                    "b.ru": ok("")})
        service = wayback.WaybackService(wayback.WaybackSettings(), self.fetcher,
                                         cache_path=Path(self.tmp.name) / "c.json")
        with self.assertRaises(wayback.ArchiveOverloaded) as ctx:
            await service.get_history("a.ru")
        self.assertEqual(ctx.exception.retry_after, 120)
        result, _ = await timed(service.get_history("b.ru"))
        self.assertIsInstance(result, wayback.ArchiveOverloaded)
        self.assertIn(result.retry_after, (119, 120))
