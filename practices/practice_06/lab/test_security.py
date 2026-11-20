"""Негативные тесты против найденных атак (практика 6).

Каждый тест описывает атаку из REPORT_01 и проверяет, что сервер её не пускает.
На исходном уязвимом service.py эти тесты падают; после исправлений — проходят.
Запуск вместе с остальными: python -m unittest -v
"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Shop, make_handler


class SecurityTest(unittest.TestCase):
    def setUp(self):
        self.shop = Shop()
        self.addCleanup(self.shop.close)

    def request(self, method, path, body=None, headers=None):
        with self.shop.lock:
            return self.shop.dispatch(method, path, body or {}, headers or {})

    def login(self, username="alice", password="learn-alice"):
        status, result = self.request("POST", "/login", {"username": username, "password": password})
        self.assertEqual(status, 200)
        return {"X-Token": result["token"]}

    # V1 — IDOR + SQLi в /profile
    def test_profile_requires_token(self):
        self.assertEqual(self.request("GET", "/profile?id=1")[0], 401)

    def test_profile_returns_only_own_ignoring_id_and_injection(self):
        auth = self.login()  # alice -> id 1
        for path in ("/profile?id=2", "/profile?id=0 OR 1=1", "/profile?id="):
            with self.subTest(path=path):
                status, rows = self.request("GET", path, headers=auth)
                self.assertEqual(status, 200)
                self.assertEqual([r["id"] for r in rows], [1])
                self.assertNotIn("TEST_BOB", [r["name"] for r in rows])

    # V2 — SQLi в /search
    def test_search_injection_does_not_leak_others(self):
        auth = self.login()
        status, rows = self.request("GET", "/search?q=' OR '1'='1' -- ", headers=auth)
        self.assertEqual(status, 200)
        self.assertNotIn("TEST_BOB", [r["name"] for r in rows])

    # V3 — /orders без аутентификации
    def test_orders_require_token_and_are_scoped(self):
        self.assertEqual(self.request("GET", "/orders")[0], 401)
        auth = self.login()
        status, rows = self.request("GET", "/orders", headers=auth)
        self.assertEqual(status, 200)
        self.assertTrue(all(r["owner"] == 1 for r in rows))

    # V4 — доверие заголовку X-Role
    def test_fake_role_header_does_not_grant_export(self):
        self.assertEqual(self.request("GET", "/admin/export", headers={"X-Role": "staff"})[0], 403)
        auth = {**self.login(), "X-Role": "staff"}  # customer + поддельный заголовок
        self.assertEqual(self.request("GET", "/admin/export", headers=auth)[0], 403)

    # V5 — подмена цены
    def test_price_taken_from_catalog(self):
        status, order = self.request("POST", "/orders", {"product": "chair", "price": 1}, self.login())
        self.assertEqual(status, 201)
        self.assertEqual(order["price"], 5000)

    # V6 — IDOR/traversal по файлам
    def test_files_scoped_to_owner(self):
        auth = self.login()  # alice
        status, result = self.request("GET", "/files?user=bob&path=receipt.txt", headers=auth)
        self.assertEqual(status, 200)
        self.assertNotIn("TEST_BOB", result["content"])  # чужой чек не отдаётся
        self.assertIn("TEST_ALICE", result["content"])

    def test_files_cannot_reach_operator_secret(self):
        auth = self.login()
        status, result = self.request("GET", "/files?user=../private&path=operator-note.txt", headers=auth)
        self.assertNotEqual(status, 200)
        self.assertNotIn("SECRET", str(result))

    # V7 — stored XSS
    def test_review_html_is_escaped(self):
        auth = self.login()
        self.request("POST", "/reviews", {"product": "chair", "text": "<script>alert(1)</script>"}, auth)
        status, page = self.request("GET", "/reviews/view", headers=auth)
        self.assertEqual(status, 200)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    # V9 — confused deputy: /assistant/execute без прав
    def test_execute_requires_token(self):
        status, _ = self.request("POST", "/assistant/execute",
                                 {"decision": {"tool": "export_profiles"}})
        self.assertEqual(status, 401)

    def test_execute_forbids_delete_tree_even_when_forced(self):
        auth = self.login()
        status, _ = self.request("POST", "/assistant/execute",
                                 {"decision": {"tool": "delete_tree", "arguments": {"path": "."}}}, auth)
        self.assertEqual(status, 403)
        # файлы на месте: свой чек по-прежнему читается
        status, result = self.request("GET", "/files?user=alice&path=receipt.txt", headers=auth)
        self.assertEqual(status, 200)
        self.assertIn("TEST_ALICE", result["content"])

    def test_execute_customer_cannot_export_profiles(self):
        auth = self.login()
        status, _ = self.request("POST", "/assistant/execute",
                                 {"decision": {"tool": "export_profiles"}}, auth)
        self.assertEqual(status, 403)

    def test_execute_read_file_cannot_reach_secret(self):
        auth = self.login()
        status, result = self.request("POST", "/assistant/execute",
                                      {"decision": {"tool": "read_file",
                                                    "arguments": {"path": "private/operator-note.txt"}}}, auth)
        self.assertNotEqual(status, 200)
        self.assertNotIn("SECRET", str(result))

    # V10 — утечка паролей и токенов в журнале
    def test_debug_logs_protected_and_scrubbed(self):
        self.assertEqual(self.request("GET", "/debug/logs")[0], 403)
        staff = self.login("staff", "learn-staff")
        status, logs = self.request("GET", "/debug/logs", headers=staff)
        self.assertEqual(status, 200)
        blob = str(logs)
        self.assertNotIn("learn-staff", blob)   # пароль не в журнале
        self.assertNotIn(staff["X-Token"], blob)  # токен не в журнале


class HttpErrorTest(unittest.TestCase):
    """V11 — проверка на реальном транспорте, где исключение превращается в 500."""

    @classmethod
    def setUpClass(cls):
        cls.shop = Shop()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.shop))
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()
        cls.shop.close()

    def test_internal_error_does_not_leak_exception(self):
        # тело-массив ломает body.get(...) внутри dispatch -> необработанное исключение
        req = Request(self.base + "/login", data=b"[]",
                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            resp = urlopen(req, timeout=5)
        except HTTPError as exc:
            resp = exc
        with resp:
            status, text = resp.status, resp.read().decode()
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(text), {"error": "internal error"})
        self.assertNotIn("AttributeError", text)
        self.assertNotIn("type", text)


if __name__ == "__main__":
    unittest.main()
