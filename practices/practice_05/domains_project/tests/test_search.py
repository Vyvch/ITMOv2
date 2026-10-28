import unittest
from unittest.mock import patch
import time
import sys
import os

from fastapi.testclient import TestClient

# Добавляем корневую папку проекта в пути для импорта
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from services import filter_domains, InvalidPatternError
from models import DomainItem
from api import app


def make_domains(*names: str) -> list[DomainItem]:
    return [DomainItem(domain=name, date="2026-10-29") for name in names]


def names(domains: list[DomainItem]) -> list[str]:
    return [d.domain for d in domains]


class TestFilterDomains(unittest.TestCase):
    def test_zone_anchor(self):
        """Якорь по зоне: \\.su$ находит только домены .su."""
        domains = make_domains("alpha.su", "beta.ru", "su-shop.ru")
        self.assertEqual(names(filter_domains(domains, r"\.su$")), ["alpha.su"])

    def test_match_in_the_middle(self):
        """Совпадение ищется в любом месте имени, а не по всей строке."""
        domains = make_domains("shop123.ru", "shop.ru")
        self.assertEqual(names(filter_domains(domains, r"[0-9]{3}")), ["shop123.ru"])

    def test_no_matches(self):
        """Валидное выражение без совпадений даёт пустой список."""
        domains = make_domains("alpha.ru", "beta.su")
        self.assertEqual(filter_domains(domains, r"^zzz"), [])

    def test_case_insensitive_latin(self):
        """Регистр латиницы не учитывается."""
        domains = make_domains("shop.ru", "market.ru")
        self.assertEqual(names(filter_domains(domains, r"^SHOP")), ["shop.ru"])

    def test_cyrillic_zone(self):
        """Кириллические домены .рф ищутся в кириллической форме."""
        domains = make_domains("пример.рф", "example.ru")
        self.assertEqual(names(filter_domains(domains, r"\.рф$")), ["пример.рф"])

    def test_case_insensitive_cyrillic(self):
        """Регистр кириллицы не учитывается."""
        domains = make_domains("пример.рф", "тест.рф")
        self.assertEqual(names(filter_domains(domains, r"^ПРИМЕР")), ["пример.рф"])

    def test_invalid_patterns_raise(self):
        """Невалидные и неподдерживаемые выражения дают InvalidPatternError с сообщением."""
        domains = make_domains("alpha.ru")
        for pattern in ["[a-", r"(a)\1", "a(?=b)"]:
            with self.subTest(pattern=pattern):
                with self.assertRaises(InvalidPatternError) as ctx:
                    filter_domains(domains, pattern)
                self.assertTrue(str(ctx.exception))

    def test_too_long_pattern_raises(self):
        """Выражение длиннее 256 символов отклоняется, 256 — допустимо."""
        domains = make_domains("alpha.ru")
        filter_domains(domains, "a" * 256)
        with self.assertRaises(InvalidPatternError):
            filter_domains(domains, "a" * 257)

    def test_catastrophic_pattern_is_fast(self):
        """(a+)+$ на длинном имени отрабатывает быстро и не находит совпадения."""
        domains = make_domains("a" * 50_000 + "!")
        start = time.perf_counter()
        result = filter_domains(domains, r"(a+)+$")
        elapsed = time.perf_counter() - start
        self.assertEqual(result, [])
        self.assertLess(elapsed, 1.0)


class TestDomainsApiRegex(unittest.TestCase):
    def setUp(self):
        # TestClient без `with`: startup-хук (запуск MCP-процесса) не срабатывает
        self.client = TestClient(app)
        domains = make_domains("alpha.su", "beta.ru", "пример.рф")
        patcher = patch("api.get_domains", return_value=domains)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_without_param_returns_full_list(self):
        """Без параметра regex ответ прежний: полный список."""
        response = self.client.get("/api/domains")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([d["domain"] for d in response.json()], ["alpha.su", "beta.ru", "пример.рф"])

    def test_empty_param_returns_full_list(self):
        """Пустой regex= работает как отсутствие параметра."""
        response = self.client.get("/api/domains", params={"regex": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 3)

    def test_valid_regex_filters_and_keeps_format(self):
        """Валидное выражение фильтрует список, формат элементов прежний."""
        response = self.client.get("/api/domains", params={"regex": r"\.su$"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [{"domain": "alpha.su", "date": "2026-10-29", "reason": None, "whois": None}],
        )

    def test_invalid_regex_returns_400(self):
        """Невалидное и неподдерживаемое выражения дают 400 с непустым detail."""
        for pattern in ["[a-", "a(?=b)"]:
            with self.subTest(pattern=pattern):
                response = self.client.get("/api/domains", params={"regex": pattern})
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.json().get("detail"))


if __name__ == "__main__":
    unittest.main()
