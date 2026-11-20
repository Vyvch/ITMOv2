import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from mcp_whois import get_ru_whois, handle_request

class TestMCPWhois(unittest.TestCase):
    @patch('mcp_whois.socket.socket')
    def test_get_ru_whois_success(self, mock_socket_class):
        # Мокаем успешный ответ сокета
        mock_socket = MagicMock()
        mock_socket_class.return_value = mock_socket
        
        # Фейковый ответ от tcinet.ru
        fake_response = (
            b"domain: YANDEX.RU\r\n"
            b"state: REGISTERED, DELEGATED, VERIFIED\r\n"
            b"registrar: RU-CENTER-RU\r\n"
            b"created: 1997-09-23T09:36:14Z\r\n"
            b"paid-till: 2026-10-01T21:00:00Z\r\n"
            b"free-date: 2026-11-02\r\n"
        )
        # recv читает всё и потом возвращает b''
        mock_socket.recv.side_effect = [fake_response, b'']
        
        result = get_ru_whois('yandex.ru')
        
        self.assertEqual(result.get('status'), 'REGISTERED, DELEGATED, VERIFIED')
        self.assertEqual(result.get('registrar'), 'RU-CENTER-RU')
        self.assertEqual(result.get('creation_date'), '1997-09-23T09:36:14Z')
        self.assertEqual(result.get('expiration_date'), '2026-10-01T21:00:00Z')
        self.assertEqual(result.get('free_date'), '2026-11-02')

    @patch('mcp_whois.socket.socket')
    def test_get_ru_whois_free(self, mock_socket_class):
        # Мокаем ответ "домен свободен"
        mock_socket = MagicMock()
        mock_socket_class.return_value = mock_socket
        mock_socket.recv.side_effect = [b"% No entries found for the selected source(s).\r\n", b'']
        
        result = get_ru_whois('nonexistentdomain123.ru')
        self.assertIn('Свободен', result.get('status', ''))

    @patch('mcp_whois.get_ru_whois')
    def test_handle_request_call(self, mock_get_whois):
        # Мокаем функцию парсера
        mock_get_whois.return_value = {"status": "TEST_STATUS", "registrar": "TEST_REG"}
        
        # Эмулируем MCP JSON-RPC запрос
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "get_whois_info",
                "arguments": {"domain": "test.ru"}
            }
        }
        
        response = handle_request(req)
        
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 1)
        self.assertFalse(response["result"]["isError"])
        
        # Проверяем, что ответ запакован в нужный формат MCP
        content_text = response["result"]["content"][0]["text"]
        parsed_content = json.loads(content_text)
        self.assertEqual(parsed_content["status"], "TEST_STATUS")

if __name__ == "__main__":
    unittest.main()
