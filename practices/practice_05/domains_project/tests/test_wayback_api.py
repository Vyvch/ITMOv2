import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

# Добавляем корневую папку проекта в пути для импорта
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import services  # noqa: E402
import wayback  # noqa: E402
from api import app  # noqa: E402
from models import WaybackHistory, WaybackSnapshot  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "wayback"
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


def make_history(count: int = 1234, domain: str = "example.ru") -> WaybackHistory:
    if count == 0:
        first = last = None
    else:
        first = WaybackSnapshot(timestamp="2009-03-15T12:34:56Z", date="2009-03-15",
                                url="https://web.archive.org/web/20090315123456/http://example.ru/")
        last = WaybackSnapshot(timestamp="2026-10-02T08:00:00Z", date="2026-10-02",
                               url="https://web.archive.org/web/20261002080000/https://example.ru/")
    return WaybackHistory(domain=domain, snapshot_count=count, count_truncated=False,
                          first=first, last=last, cached=False, checked_at="2026-10-28T18:40:12Z")


class FakeFetcher:
    """Фейковый загрузчик CDX: всегда отвечает образцом и считает вызовы."""

    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    async def __call__(self, ascii_domain: str, limit: int) -> wayback.FetchResult:
        self.calls += 1
        return wayback.FetchResult(status=200, headers={}, text=self.text)


class WaybackApiTestCase(unittest.TestCase):
    def setUp(self):
        services.reset_service()
        self.tmp = tempfile.TemporaryDirectory()
        self.client = TestClient(app)

    def tearDown(self):
        services.reset_service()
        self.tmp.cleanup()

    def real_service(self) -> tuple[wayback.WaybackService, FakeFetcher]:
        """Настоящий сервис с фейковым загрузчиком, подставленный как модульный."""
        fetcher = FakeFetcher((FIXTURES / "cdx_regular.txt").read_text(encoding="utf-8"))
        service = wayback.WaybackService(
            wayback.WaybackSettings(request_interval=0.01), fetcher,
            cache_path=Path(self.tmp.name) / "wayback_cache.json",
        )
        services.reset_service(service)
        return service, fetcher

    def assert_error_body(self, response, code: str) -> None:
        body = response.json()
        self.assertEqual(set(body), {"code", "detail"})
        self.assertEqual(body["code"], code)
        self.assertTrue(body["detail"])
        self.assertNotIn("Traceback", response.text)


class TestWaybackEndpointUS1(WaybackApiTestCase):
    """Эндпоинт GET /api/wayback по контракту (C1, C2, C4, C5, C9, C10, C11, C13)."""

    def test_c1_history(self):
        with patch("api.get_wayback_history", new_callable=AsyncMock, return_value=make_history()):
            response = self.client.get("/api/wayback", params={"domain": "example.ru"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"domain", "snapshot_count", "count_truncated", "first", "last",
                                     "cached", "checked_at"})
        self.assertEqual(body["snapshot_count"], 1234)
        self.assertRegex(body["first"]["date"], DATE_RE)
        self.assertRegex(body["last"]["date"], DATE_RE)
        self.assertEqual(set(body["first"]), {"timestamp", "date", "url"})

    def test_c2_no_snapshots(self):
        with patch("api.get_wayback_history", new_callable=AsyncMock,
                   return_value=make_history(count=0)):
            response = self.client.get("/api/wayback", params={"domain": "example.ru"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["snapshot_count"], 0)
        self.assertIsNone(body["first"])
        self.assertIsNone(body["last"])

    def test_c4_invalid_domain(self):
        with patch("api.get_wayback_history", new_callable=AsyncMock,
                   side_effect=wayback.InvalidDomain()):
            response = self.client.get("/api/wayback", params={"domain": "example.com"})
        self.assertEqual(response.status_code, 400)
        self.assert_error_body(response, "invalid_domain")
        self.assertNotIn("retry-after", response.headers)

    def test_c5_missing_parameter(self):
        """Без параметра — 400 invalid_domain, а не 422 FastAPI (настоящий сервис)."""
        self.real_service()
        response = self.client.get("/api/wayback")
        self.assertEqual(response.status_code, 400)
        self.assert_error_body(response, "invalid_domain")

    def test_c10_raw_domain_passed_to_service(self):
        mock = AsyncMock(return_value=make_history(domain="пример.рф"))
        with patch("api.get_wayback_history", mock):
            self.client.get("/api/wayback", params={"domain": "пример.рф"})
        mock.assert_awaited_once_with("пример.рф")

    def test_c11_two_requests_without_context_manager(self):
        """Каждый запрос TestClient без with — новый цикл событий; оба должны отработать."""
        _, fetcher = self.real_service()
        first = self.client.get("/api/wayback", params={"domain": "example.ru"})
        second = self.client.get("/api/wayback", params={"domain": "example.ru"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["snapshot_count"], 194)
        # US3: второй ответ — из кэша, архив вызван один раз
        self.assertFalse(first.json()["cached"])
        self.assertTrue(second.json()["cached"])
        self.assertEqual(fetcher.calls, 1)

    def test_c13_shutdown_stops_worker(self):
        """Обработчик shutdown приложения останавливает работника сервиса."""
        service, _ = self.real_service()
        with patch("api.mcp_client"):
            with TestClient(app) as client:
                response = client.get("/api/wayback", params={"domain": "example.ru"})
                self.assertEqual(response.status_code, 200)
                self.assertFalse(service.worker.done())
        self.assertTrue(service.worker.done())


class TestWaybackEndpointErrors(WaybackApiTestCase):
    """Ошибки архива и формат ответов (C6–C9, C12)."""

    def get_with(self, **mock_kwargs):
        with patch("api.get_wayback_history", new_callable=AsyncMock, **mock_kwargs):
            return self.client.get("/api/wayback", params={"domain": "example.ru"})

    def test_c6_overloaded(self):
        response = self.get_with(side_effect=wayback.ArchiveOverloaded(retry_after=60))
        self.assertEqual(response.status_code, 503)
        self.assert_error_body(response, "archive_overloaded")
        self.assertEqual(response.headers["retry-after"], "60")

    def test_c7_timeout(self):
        response = self.get_with(side_effect=wayback.ArchiveTimeout())
        self.assertEqual(response.status_code, 504)
        self.assert_error_body(response, "archive_timeout")
        self.assertNotIn("retry-after", response.headers)

    def test_c8_archive_error(self):
        response = self.get_with(side_effect=wayback.ArchiveUnavailable())
        self.assertEqual(response.status_code, 502)
        self.assert_error_body(response, "archive_error")
        self.assertNotIn("Exception", response.text)

    def test_c12_always_json(self):
        outcomes = {
            "ok": {"return_value": make_history()},
            "empty": {"return_value": make_history(count=0)},
            "pending": {"side_effect": wayback.HistoryPending("example.ru", 5)},
            "invalid": {"side_effect": wayback.InvalidDomain()},
            "error": {"side_effect": wayback.ArchiveUnavailable()},
            "overloaded": {"side_effect": wayback.ArchiveOverloaded(retry_after=3)},
            "timeout": {"side_effect": wayback.ArchiveTimeout()},
        }
        for label, kwargs in outcomes.items():
            with self.subTest(label=label):
                response = self.get_with(**kwargs)
                self.assertTrue(response.headers["content-type"].startswith("application/json"))
                response.json()
        self.real_service()
        response = self.client.get("/api/wayback")
        self.assertTrue(response.headers["content-type"].startswith("application/json"))
        response.json()


class TestWaybackEndpointPending(WaybackApiTestCase):
    """Ответ 202 «в очереди» (C3)."""

    def test_c3_pending(self):
        for domain, retry_after in [("example.ru", 8), ("пример.рф", 3)]:
            with self.subTest(domain=domain):
                with patch("api.get_wayback_history", new_callable=AsyncMock,
                           side_effect=wayback.HistoryPending(domain, retry_after)):
                    response = self.client.get("/api/wayback", params={"domain": domain})
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json(), {"domain": domain, "status": "pending",
                                                   "retry_after": retry_after})
                self.assertEqual(response.headers["retry-after"], str(retry_after))


if __name__ == "__main__":
    unittest.main()
