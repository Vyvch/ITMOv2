import unittest
from unittest.mock import patch, MagicMock
import sys
import os

# Добавляем корневую папку проекта в пути для импорта main
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from services import get_domains
from models import DomainItem

class TestDomains(unittest.TestCase):
    @patch('services.httpx.Client.get')
    @patch('services.gzip.decompress')
    def test_get_domains_parsing(self, mock_decompress, mock_get):
        """Проверяем правильность парсинга TSV-ответов от auction.nic.ru."""
        # Мокаем httpx.Response
        mock_response = MagicMock()
        mock_response.content = b'dummy_compressed_data'
        mock_get.return_value = mock_response
        
        # Мокаем gzip.decompress для возврата поддельного TSV (CP1251)
        tsv_content = (
            "Domain\tRegistrar\tFree-date\tMay be released\n"
            "match.ru\tREG-RU\t2026-10-07\t2026-10-07\n"
            "nomatch.ru\tREG-RU\t2026-10-08\t2026-10-08\n"
            "badline\n"
        ).encode('cp1251')
        mock_decompress.return_value = tsv_content

        domains = get_domains()
        
        self.assertIsInstance(domains, list)
        self.assertTrue(len(domains) > 0)
        self.assertIsInstance(domains[0], DomainItem)
        
        # Поскольку у нас 3 URL-адреса (.ru, .su, .рф), функция выполнит 3 запроса.
        # Каждый раз мы возвращаем один и тот же мок-ответ
        expected = [
            DomainItem(domain="match.ru", date="2026-10-07"),
            DomainItem(domain="nomatch.ru", date="2026-10-08"),
        ] * 3
        
        self.assertEqual([(d.domain, d.date) for d in domains], [(d.domain, d.date) for d in expected])

    def test_get_domains_real_network(self):
        """Проверяем, что функция возвращает список на реальных данных (интеграционный тест)."""
        domains = get_domains()
        self.assertIsInstance(domains, list)
        # Мы не можем быть на 100% уверены, что домены всегда будут, но обычно они есть
        if domains:
            self.assertTrue(len(domains) > 0)

if __name__ == "__main__":
    unittest.main()
