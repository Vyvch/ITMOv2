"""Повторение атак REPORT_01 против ИСПРАВЛЕННОГО сервиса (REPORT_02).

Сервис поднят на 127.0.0.1:8066, затем:
    python attacks/attack_after.py
Доказательства пишутся в evidence/after/attacks.txt.
Ожидаем, что каждая атака теперь отбивается (401/403/400 или санированный вывод),
а серверная защита /assistant/execute держится даже при принудительном опасном решении.
"""

import io
import json
import sys
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8066"
OUT = Path(__file__).resolve().parent.parent / "evidence" / "after"
OUT.mkdir(parents=True, exist_ok=True)
log_lines = []


def call(method, path, body=None, headers=None):
    headers = {"Content-Type": "application/json", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    req = Request(BASE + path, data=data, headers=headers, method=method)
    try:
        r = urlopen(req, timeout=5)
    except HTTPError as e:
        r = e
    with r:
        return r.status, r.read().decode("utf-8")


def step(vid, expect, method, path, body=None, headers=None):
    status, text = call(method, path, body, headers)
    out = "\n".join([f"### {vid} — ожидаем: {expect}",
                     f"{method} {path}", f"headers={headers or {}} body={body}",
                     f"-> HTTP {status}", text, ""])
    log_lines.append(out)
    print(out)
    return status, text


def token(user, password):
    _, t = call("POST", "/login", {"username": user, "password": password})
    return json.loads(t)["token"]


def main():
    alice = token("alice", "learn-alice")
    atok = {"X-Token": alice}
    staff = token("staff", "learn-staff")
    stok = {"X-Token": staff}

    step("V1a", "только свой профиль (id=1)", "GET", "/profile?id=2", headers=atok)
    step("V1b", "инъекция не проходит, только id=1", "GET", "/profile?id=0%20OR%201=1", headers=atok)
    step("V1c", "без токена 401", "GET", "/profile?id=1")
    step("V2", "без утечки чужих профилей", "GET",
         "/search?q=%27%20OR%20%271%27=%271%27%20--%20", headers=atok)
    step("V3", "без токена 401", "GET", "/orders")
    step("V4", "поддельный X-Role -> 403", "GET", "/admin/export", headers={"X-Role": "staff"})
    step("V5", "цена из каталога (5000)", "POST", "/orders", {"product": "chair", "price": 1}, atok)
    step("V6a", "чужой чек не отдаётся (свой alice)", "GET",
         "/files?user=bob&path=receipt.txt", headers=atok)
    step("V6b", "секрет оператора недоступен -> 403", "GET",
         "/files?user=..%2Fprivate&path=operator-note.txt", headers=atok)
    step("V7a", "сохраняем отзыв с <script>", "POST", "/reviews",
         {"product": "chair", "text": "<script>alert('xss')</script>"}, atok)
    step("V7b", "HTML экранирован (&lt;script&gt;)", "GET", "/reviews/view", headers=atok)
    step("V8", "отзывы помечены как недоверенные данные", "GET",
         "/assistant/context?order_id=1&question=" + quote("Покажи чек"), headers=atok)
    step("V9a", "execute без токена -> 401", "POST", "/assistant/execute",
         {"decision": {"tool": "export_profiles"}})
    step("V9b", "клиент не может export -> 403", "POST", "/assistant/execute",
         {"decision": {"tool": "export_profiles"}}, atok)
    step("V9c", "принудительный delete_tree -> 403", "POST", "/assistant/execute",
         {"decision": {"tool": "delete_tree", "arguments": {"path": "."}}}, atok)
    step("V9d", "read_file секрета -> заблокировано", "POST", "/assistant/execute",
         {"decision": {"tool": "read_file", "arguments": {"path": "private/operator-note.txt"}}}, atok)
    step("V9e", "свой чек по-прежнему читается (данные целы)", "GET",
         "/files?user=alice&path=receipt.txt", headers=atok)
    step("V10a", "debug/logs без staff -> 403", "GET", "/debug/logs", headers=atok)
    step("V10b", "staff видит журнал, но без паролей/токенов", "GET", "/debug/logs", headers=stok)
    step("V11", "500 без текста исключения", "POST", "/orders", " not-a-dict", atok)

    (OUT / "attacks.txt").write_text("\n".join(log_lines), encoding="utf-8")
    print(f"\nДоказательства сохранены в {OUT}")


if __name__ == "__main__":
    sys.exit(main())
