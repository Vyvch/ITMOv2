# Образцы ответов Wayback CDX

Сняты один раз вручную 2026-10-28 (задача T001, research R1). Тесты используют только эти файлы,
сеть в тестах не нужна.

## Запросы

| Файл | Адрес | HTTP | Время |
|------|-------|------|-------|
| `cdx_regular.txt` | `https://web.archive.org/cdx/search/cdx?url=example.ru&fl=timestamp,original&limit=100000` | 200 | 2,1 с (первая проба этого же запроса — 17,5 с) |
| `cdx_empty.txt` | `https://web.archive.org/cdx/search/cdx?url=zq7x9k2m4p8w.ru&fl=timestamp,original&limit=100000` | 200 | 1,9 с |
| `cdx_last.txt` | `https://web.archive.org/cdx/search/cdx?url=example.ru&fl=timestamp,original&limit=-1&fastLatest=true` | 200 | 2,1 с |

Значимые заголовки во всех трёх ответах: `Content-Type: text/plain`, `Retry-After` нет.

## Содержимое

- `cdx_regular.txt` — 194 строки, 7208 байт, по одной строке `<14 цифр> <исходный адрес>` через пробел,
  по возрастанию времени, с завершающим переводом строки. Все отметки — ровно 14 цифр.
  - первая строка: `20010330041232 http://www.example.ru:80/`
  - последняя строка: `20230510085431 http://example.ru/`
  - в одном наборе встречаются `http://www.example.ru:80/`, `http://example.ru:80/`, `http://example.ru/` —
    канонизация архива объединяет `www.`, порт и схему (подтверждает research R1).
- `cdx_empty.txt` — пустое тело при 200 (домен без снимков; подтверждает research R2).
- `cdx_last.txt` — одна строка `20230510085431 http://example.ru/`: `limit=-1&fastLatest=true` отдаёт
  последний снимок, он совпадает с последней строкой `cdx_regular.txt`.
- `cdx_blocked.txt` — не снят: исключённый из архива сайт не попался. Признак из research R2
  (403 + `AdministrativeAccessControlException` / `Blocked Site`) остаётся допущением и проверяется
  синтетической строкой.

## Наблюдения

- Первый запрос `example.ru` шёл 17,5 с — дольше таймаута 10 с (FR-021); повтор — 2,1 с. Холодные
  ответы архива бывают медленными: на реальном архиве часть первых запросов может завершаться
  `archive_timeout`, повтор обычно успевает. Учесть при ручной проверке (T034).

## Ручная проверка (T034, 2026-10-28/29)

Сервер `uvicorn api:app` (порт 8001, 8000 был занят), запросы — скриптом через `httpx`.

| Сценарий | Результат |
|----------|-----------|
| M1 `example.ru` | 200, 194 снимка, 2001-03-30 … 2023-05-10, `cached: false`, 1,8 с |
| M2 повтор | 200, `cached: true`, 0,0 с |
| M3 `пример.рф` / `xn--e1afmkfd.xn--p1ai` | 200, 2 снимка, `domain` кириллицей; punycode — из той же записи кэша |
| M4 `zq7x9k2m4p8w.ru` | 200, `snapshot_count: 0`, `first`/`last` = `null` |
| M5 `example.com`, `www.example.ru`, без параметра | 400 `invalid_domain` |
| M6 20 доменов из списка освобождающихся, одновременно | за 15,4 с: 6 × 200, 14 × 202 (`Retry-After` 4…100 с, растёт с позицией); через ~3 мин 17 в кэше, 3 — `202`: архив начал отклонять TCP-соединения (`All connection attempts failed`) и отклонял несколько минут, затем снова отвечал 200 |
| M6 повтор после перезапуска | 20 × 200 за 12 с (17 из кэша, 3 догружены) |
| M7 перезапуск сервера | `example.ru` — 200, `cached: true`: кэш прочитан из `.cache/wayback_cache.json` |
| `git status` | `.cache/wayback_cache.json` не виден (правило `.cache` в `.gitignore`) |

Находки:
- Python 3.14 писал в журнал трассировку «exception in shielded future», когда задача после 202
  завершалась ошибкой. Исправлено: ожидание через `asyncio.wait` без `shield` (research R4),
  регрессионный тест `test_background_failure_after_pending_is_quiet`.
- Архив блокирует соединения после пачки запросов даже при интервале 1 с (в первом прогоне M6
  было ~25 обращений за ~1 мин вместе с повторами клиента). Интервал — настройка
  `request_interval` (FR-017); его увеличение — решение по спецификации.
  Решение (2026-10-29): `request_interval` по умолчанию поднят до 3 с — 20 обращений в минуту
  (FR-017, research R12).
