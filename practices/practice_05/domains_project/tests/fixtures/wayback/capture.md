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
