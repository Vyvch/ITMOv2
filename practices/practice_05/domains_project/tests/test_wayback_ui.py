import json
import os
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from typing import Any

INDEX_HTML = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'templates', 'index.html'))
NODE = shutil.which("node")

# Исполняет ядро и сценарий в изолированном контексте vm. Новому контексту не хватает веб-API,
# которыми пользуется ядро, поэтому URL и AbortController передаются явно.
NODE_RUNNER = r"""
const vm = require('vm');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', async () => {
    const { core, scenario } = JSON.parse(input);
    const context = vm.createContext({ URL, AbortController, console });
    try {
        vm.runInContext(core, context);
        const run = vm.runInContext('(async () => {\n' + scenario + '\n})', context);
        const result = await run();
        process.stdout.write(JSON.stringify(result === undefined ? null : result));
    } catch (e) {
        process.stderr.write(String((e && e.stack) || e));
        process.exit(1);
    }
});
"""


# Общие помощники сценариев: виртуальные часы, управляемый fetch и готовые deps загрузчика.
SCENARIO_HELPERS = r"""
async function flush() { for (let i = 0; i < 50; i++) await Promise.resolve(); }

function makeClock() {
    let now = 0, seq = 0;
    const timers = new Map();
    return {
        now: () => now,
        setTimer(fn, ms) { const id = ++seq; timers.set(id, { at: now + ms, fn }); return id; },
        clearTimer(id) { timers.delete(id); },
        pending: () => timers.size,
        async advance(ms) {
            const end = now + ms;
            for (;;) {
                await flush();
                let next = null;
                for (const [id, t] of timers) {
                    if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
                }
                if (!next) break;
                timers.delete(next[0]);
                now = next[1].at;
                next[1].fn();
            }
            now = end;
            await flush();
        },
    };
}

function makeNet(clock) {
    const calls = [];
    const fetchFn = (url, { signal }) => new Promise((resolve, reject) => {
        const call = {
            url, signal, at: clock.now(), done: false,
            respond(status, body, headers = {}) {
                call.done = true;
                resolve({
                    status,
                    headers: { get: name => (name in headers ? headers[name] : null) },
                    text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
                });
            },
            fail() { call.done = true; reject(new Error('network')); },
        };
        signal.addEventListener('abort', () => { call.aborted = true; reject(new Error('aborted')); });
        calls.push(call);
    });
    return { calls, fetchFn, inFlight: () => calls.filter(c => !c.done && !c.aborted).length };
}

function makeDeps(clock, net, overrides = {}) {
    const states = [];
    const deps = {
        fetchFn: net.fetchFn, now: clock.now, setTimer: clock.setTimer, clearTimer: clock.clearTimer,
        limiter: WaybackCore.createLimiter(4), memo: WaybackCore.createMemo(),
        onState: s => states.push(s), ...overrides,
    };
    return { deps, states, kinds: () => states.map(s => s.kind) };
}

const SNAPSHOT_BODY = {
    domain: 'example.ru', snapshot_count: 1234, count_truncated: false,
    first: { timestamp: '2009-03-15T12:34:56Z', date: '2009-03-15',
             url: 'https://web.archive.org/web/20090315123456/http://example.ru/' },
    last: { timestamp: '2026-10-02T08:00:00Z', date: '2026-10-02',
            url: 'https://web.archive.org/web/20261002080000/https://example.ru/' },
    cached: false, checked_at: '2026-10-28T18:40:12Z',
};
"""


class _CoreScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[str] = []
        self._inside = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and ("id", "wayback-core") in attrs:
            self._inside = True
            self.blocks.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._inside = False

    def handle_data(self, data: str) -> None:
        if self._inside:
            self.blocks[-1] += data


def read_index_html() -> str:
    with open(INDEX_HTML, encoding="utf-8") as f:
        return f.read()


def core_blocks() -> list[str]:
    parser = _CoreScriptParser()
    parser.feed(read_index_html())
    return parser.blocks


def load_core_source() -> str:
    blocks = core_blocks()
    if not blocks:
        raise AssertionError('В templates/index.html нет блока <script id="wayback-core">')
    return blocks[0]


def run_core(scenario_js: str) -> Any:
    """Выполняет ядро WaybackCore и асинхронный сценарий в node; возвращает результат сценария."""
    payload = json.dumps({"core": load_core_source(), "scenario": SCENARIO_HELPERS + scenario_js})
    proc = subprocess.run(
        [NODE, "-e", NODE_RUNNER], input=payload, capture_output=True,
        text=True, encoding="utf-8", timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"Сценарий в node упал:\n{proc.stderr}")
    return json.loads(proc.stdout)


def _scan(text: str, start: int, stop_chars: str) -> int:
    """Индекс первого символа из stop_chars на верхнем уровне (вне строк и скобок) или конец текста."""
    depth = 0
    quote = None
    i = start
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif depth == 0 and ch in stop_chars:
            return i
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        i += 1
    return len(text)


def fetch_first_args(text: str) -> list[str]:
    """Первые аргументы всех вызовов fetch(...) в тексте."""
    args = []
    for match in re.finditer(r"\bfetch\(", text):
        end = _scan(text, match.end(), ",)")
        args.append(text[match.end():end].strip())
    return args


def function_body(text: str, name: str) -> str:
    """Тело JS-функции name из основного скрипта страницы (от объявления до следующей функции)."""
    match = re.search(rf"function {name}\(.*?(?=\n\s*(?:async )?function |\n\s*</script>)", text, re.S)
    if not match:
        raise AssertionError(f"В templates/index.html нет функции {name}")
    return match.group(0)


def tag_with(text: str, tag: str, attr: str) -> str:
    """Открывающий тег tag, содержащий атрибут attr (например, 'id="domain-dialog"')."""
    match = re.search(rf"<{tag}\b[^>]*{re.escape(attr)}[^>]*>", text)
    if not match:
        raise AssertionError(f"Нет тега <{tag}> с {attr}")
    return match.group(0)


def concat_parts(expr: str) -> list[str]:
    """Слагаемые выражения, разделённые '+' на верхнем уровне."""
    parts = []
    pos = 0
    while True:
        end = _scan(expr, pos, "+")
        parts.append(expr[pos:end].strip())
        if end == len(expr):
            return parts
        pos = end + 1


class StaticChecks(unittest.TestCase):
    """Проверки текста templates/index.html; работают без node."""

    def test_fetch_only_relative_api(self):
        """Клиент обращается только к своему серверу: fetch начинается с относительного /api/ (принцип IV)."""
        args = fetch_first_args(read_index_html())
        self.assertTrue(args)
        for arg in args:
            with self.subTest(arg=arg):
                self.assertRegex(arg, r"^['\"`]/api/")

    def test_api_params_encoded(self):
        """Каждый подставляемый параметр запроса обёрнут в encodeURIComponent (принцип V)."""
        for arg in fetch_first_args(read_index_html()):
            with self.subTest(arg=arg):
                for placeholder in re.finditer(r"\$\{", arg):
                    self.assertTrue(arg.startswith("encodeURIComponent(", placeholder.end()))
                for part in concat_parts(arg)[1:]:
                    self.assertTrue(part.startswith("encodeURIComponent("))

    def test_core_requests_only_own_api(self):
        """Ядро ходит только в /api/wayback, домен закодирован (принципы IV и V)."""
        core = load_core_source()
        self.assertNotRegex(core, r"\bfetch\(")
        self.assertIn("'/api/wayback?domain=' + encodeURIComponent(domain)", core)
        self.assertNotIn("http://web.archive.org", core.replace("'http://' + name", ""))
        self.assertEqual(re.findall(r"deps\.fetchFn\((\w+)", core), ["url"])

    def test_core_block_present(self):
        """Блок ядра <script id="wayback-core"> ровно один."""
        self.assertEqual(len(core_blocks()), 1)

    def test_domain_dialog_anchors(self):
        """Карточка домена размечена по contracts/ui-contract.md §2."""
        html = read_index_html()
        dialog = tag_with(html, "dialog", 'id="domain-dialog"')
        self.assertIn('aria-labelledby="domain-dialog-title"', dialog)
        self.assertIn('id="domain-dialog-title"', html)
        self.assertIn('id="domain-dialog-date"', html)
        link = tag_with(html, "a", 'class="wayback-link"')
        self.assertIn('target="_blank"', link)
        self.assertIn('rel="noopener noreferrer"', link)
        tag_with(html, "button", 'class="domain-dialog-close"')
        tag_with(html, "button", 'class="wayback-request"')
        history = tag_with(html, "div", 'class="wayback-history"')
        self.assertIn('role="status"', history)

    def test_history_texts(self):
        """Тексты блока истории из contracts/ui-contract.md §3 и кнопка повтора (FR-008…FR-012c)."""
        html = read_index_html()
        for text in ["Загрузка истории…", "Снимков: ", "Первый: ", "Последний: ", "Нет снимков в веб-архиве",
                     "Веб-архив перегружен, попробуйте позже", "Веб-архив не ответил вовремя",
                     "Не удалось получить историю", "'Повторить'", "Повторить через "]:
            with self.subTest(text=text):
                self.assertIn(text, html)
        self.assertIn("wayback-retry", html)

    def test_no_html_insertion(self):
        """Нет HTML-вставок: outerHTML, insertAdjacentHTML, document.write; innerHTML — только очистка (FR-020)."""
        html = read_index_html()
        for forbidden in ["outerHTML", "insertAdjacentHTML", "document.write"]:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, html)
        for match in re.finditer(r"innerHTML\s*=(?!\s*'')", html):
            with self.subTest(at=html[match.start():match.start() + 60]):
                self.fail("innerHTML присваивается не пустой строкой")

    def test_top_table_has_wayback_column(self):
        """Четвёртая колонка Топ-20 — «Веб-архив» (FR-017)."""
        headers = re.findall(r"<th>(.*?)</th>", read_index_html())
        self.assertEqual(headers, ["Домен", "Обоснование LLM", "WHOIS Инфо", "Веб-архив"])

    def test_top_rows_do_not_start_history(self):
        """Отрисовка Топ-20 не запускает загрузку истории: только блок с кнопкой (FR-018)."""
        html = read_index_html()
        self.assertIn("waybackCell(", function_body(html, "loadTop"))
        self.assertIn("createHistoryBlock(", function_body(html, "waybackCell"))
        for name in ["loadTop", "waybackCell"]:
            with self.subTest(function=name):
                body = function_body(html, name)
                self.assertNotIn("createHistoryLoader(", body)
                self.assertNotIn(".start(", body)

    def test_domain_cards_are_buttons(self):
        """Карточки сетки — кнопки, имя и дата выводятся как текст (FR-001, FR-022)."""
        body = function_body(read_index_html(), "renderDomains")
        self.assertRegex(body, r"el\('button',\s*\{[^}]*className:\s*'domain-card'")
        self.assertNotIn("createElement('div')", body)
        self.assertNotRegex(body, r"innerHTML\s*=(?!\s*'')")


class CoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        if NODE is None:
            self.skipTest("node не найден в PATH: тесты WaybackCore пропущены")


class TestCoreSetup(CoreTestCase):
    def test_core_exposes_constants(self):
        """Ядро публикует лимиты ожидания и параллельности."""
        result = run_core("""
            const c = globalThis.WaybackCore;
            return [c.DEADLINE_MS, c.REQUEST_TIMEOUT_MS, c.MAX_PARALLEL];
        """)
        self.assertEqual(result, [120000, 20000, 4])


class TestArchiveUrl(CoreTestCase):
    def test_archive_url(self):
        """Ссылка на все снимки домена; .рф — в punycode, регистр не важен (FR-005)."""
        cases = {
            "пример.рф": "https://web.archive.org/web/*/xn--e1afmkfd.xn--p1ai",
            "Example.RU": "https://web.archive.org/web/*/example.ru",
            "alpha.su": "https://web.archive.org/web/*/alpha.su",
        }
        result = run_core(f"""
            return {json.dumps(list(cases))}.map(d => globalThis.WaybackCore.archiveUrl(d));
        """)
        self.assertEqual(result, list(cases.values()))

    def test_archive_url_never_throws(self):
        """Не имя хоста не ломает отрисовку: кодируется целиком и не уводит на другой хост."""
        names = ["xss<img src=x onerror=alert(1)>.ru", "a/b.ru", "a b.ru", "a..b.ru"]
        result = run_core(f"""
            return {json.dumps(names)}.map(d => WaybackCore.archiveUrl(d));
        """)
        self.assertEqual(result[:3], [
            "https://web.archive.org/web/*/xss%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E.ru",
            "https://web.archive.org/web/*/a%2Fb.ru",
            "https://web.archive.org/web/*/a%20b.ru",
        ])
        self.assertTrue(result[3].startswith("https://web.archive.org/web/*/"))


class TestFormatting(CoreTestCase):
    def test_format_date(self):
        """ГГГГ-ММ-ДД → ДД.ММ.ГГГГ перестановкой, иначе прочерк (FR-009a)."""
        result = run_core("""
            return ['2009-03-15', '2009-3-15', '', null].map(t => WaybackCore.formatDate(t));
        """)
        self.assertEqual(result, ["15.03.2009", "—", "—", "—"])

    def test_format_count(self):
        """Разряды через неразрывный пробел; урезанное число с «+» (FR-009)."""
        result = run_core("""
            return [WaybackCore.formatCount(1234, false), WaybackCore.formatCount(100000, true),
                    WaybackCore.formatCount(0, false)];
        """)
        self.assertEqual(result, ["1 234", "100 000+", "0"])

    def test_safe_href(self):
        """Ссылка на снимок — только https://web.archive.org/... (FR-010)."""
        good = "https://web.archive.org/web/1/x"
        bad = ["http://web.archive.org/web/1/x", "https://a.ru/", "https://web.archive.org.evil.ru/",
               "javascript:alert(1)", "", None, "not a url"]
        result = run_core(f"""
            return [{json.dumps(good)}, ...{json.dumps(bad)}].map(u => WaybackCore.safeHref(u));
        """)
        self.assertEqual(result, [good] + [None] * len(bad))


class TestClassifyResponse(CoreTestCase):
    def classify(self, cases: list[tuple[int, str | None, Any]]) -> list[dict]:
        bodies = [(status, header, body if isinstance(body, str) else json.dumps(body))
                  for status, header, body in cases]
        return run_core(f"""
            return {json.dumps(bodies)}.map(([s, h, b]) => WaybackCore.classifyResponse(s, h, b));
        """)

    def test_success_and_empty(self):
        """200: история, урезанное число, «нет снимков» (FR-009, FR-011)."""
        body = {"snapshot_count": 1234, "count_truncated": False,
                "first": {"date": "2009-03-15", "url": "https://web.archive.org/web/20090315123456/http://example.ru/"},
                "last": {"date": "2026-10-02", "url": "https://web.archive.org/web/20261002080000/https://example.ru/"}}
        success, truncated, empty = self.classify([
            (200, None, body),
            (200, None, {**body, "snapshot_count": 100000, "count_truncated": True}),
            (200, None, {"snapshot_count": 0, "count_truncated": False, "first": None, "last": None}),
        ])
        self.assertEqual(success, {
            "kind": "success", "count": 1234, "truncated": False,
            "first": {"dateText": "15.03.2009", "href": body["first"]["url"]},
            "last": {"dateText": "02.10.2026", "href": body["last"]["url"]},
        })
        self.assertEqual((truncated["kind"], truncated["count"], truncated["truncated"]), ("success", 100000, True))
        self.assertEqual(empty, {"kind": "empty"})

    def test_bad_success_bodies(self):
        """200 без целого неотрицательного числа → прочая ошибка; плохие снимки → null/без ссылки."""
        snap = {"date": "2009-03-15", "url": "https://web.archive.org/web/1/x"}
        results = self.classify([
            (200, None, {"snapshot_count": "5"}),
            (200, None, {"snapshot_count": -1}),
            (200, None, {"snapshot_count": 1.5}),
            (200, None, {}),
            (200, None, {"snapshot_count": 3, "first": None, "last": snap}),
            (200, None, {"snapshot_count": 3, "first": {**snap, "url": "javascript:alert(1)"}, "last": snap}),
            (200, None, {"snapshot_count": 3, "first": {**snap, "url": "https://a.ru/"}, "last": snap}),
        ])
        self.assertEqual([r["kind"] for r in results[:4]], ["failed"] * 4)
        self.assertIsNone(results[4]["first"])
        self.assertIsNone(results[5]["first"]["href"])
        self.assertIsNone(results[6]["first"]["href"])

    def test_pending(self):
        """202: повтор через retry_after, иначе Retry-After, иначе 1 с (FR-013)."""
        results = self.classify([
            (202, "5", {"status": "pending", "retry_after": 8}),
            (202, "5", {"status": "pending"}),
            (202, None, {"status": "pending"}),
        ])
        self.assertEqual(results, [{"kind": "pending", "retryAfterSec": s} for s in (8, 5, 1)])

    def test_errors_by_code(self):
        """Ошибки различаются по code, detail не влияет (FR-012, FR-012a)."""
        results = self.classify([
            (503, "12", {"code": "archive_overloaded", "detail": "что угодно"}),
            (503, None, {"code": "archive_overloaded"}),
            (503, "abc", {"code": "archive_overloaded"}),
            (503, "0", {"code": "archive_overloaded"}),
            (504, None, {"code": "archive_timeout", "detail": "Некорректный домен"}),
            (502, None, {"code": "archive_error"}),
            (400, None, {"code": "invalid_domain"}),
            (400, None, {"code": "something_new"}),
            (503, "5", {"code": "archive_timeout"}),
            (500, None, {"code": "archive_error"}),
            (418, None, {}),
            (502, None, "<html>Bad Gateway</html>"),
        ])
        self.assertEqual(results[:7], [
            {"kind": "overloaded", "retryAfterSec": 12},
            {"kind": "overloaded", "retryAfterSec": None},
            {"kind": "overloaded", "retryAfterSec": None},
            {"kind": "overloaded", "retryAfterSec": None},
            {"kind": "timeout", "reason": "server"},
            {"kind": "failed"},
            {"kind": "invalid"},
        ])
        self.assertEqual(results[7:], [{"kind": "failed"}] * 5)


class TestMemoLimiterCountdown(CoreTestCase):
    def test_memo_keeps_only_final_results(self):
        """В памяти только успех и «нет снимков», ключ без учёта регистра (FR-015)."""
        result = run_core("""
            const memo = WaybackCore.createMemo();
            memo.put('Example.RU', { kind: 'success', count: 1 });
            memo.put('empty.ru', { kind: 'empty' });
            for (const kind of ['pending', 'overloaded', 'timeout', 'failed', 'invalid']) memo.put(kind + '.ru', { kind });
            return [memo.get('example.ru'), memo.get('EMPTY.RU'),
                    ...['pending', 'overloaded', 'timeout', 'failed', 'invalid'].map(k => memo.get(k + '.ru') ?? null)];
        """)
        self.assertEqual(result, [{"kind": "success", "count": 1}, {"kind": "empty"}] + [None] * 5)

    def test_limiter_runs_at_most_max_in_fifo_order(self):
        """Не больше max задач одновременно, остальные по очереди; отменённая из очереди не стартует."""
        result = run_core("""
            const limiter = WaybackCore.createLimiter(2);
            const started = [], finish = {};
            const task = name => () => new Promise(r => { started.push(name); finish[name] = r; });
            const aborted = new AbortController();
            limiter.run(task('a')); limiter.run(task('b'));
            limiter.run(task('c'), aborted.signal).catch(() => started.push('c-rejected'));
            limiter.run(task('d'));
            await flush();
            const before = [...started];
            aborted.abort();
            await flush();
            finish.a();
            await flush();
            return { before, after: started };
        """)
        self.assertEqual(result["before"], ["a", "b"])
        self.assertEqual(result["after"], ["a", "b", "c-rejected", "d"])

    def test_countdown(self):
        """Отсчёт 3, 2, 1, 0 раз в секунду; после cancel() тиков нет (FR-012b)."""
        result = run_core("""
            const clock = makeClock();
            const ticks = [];
            WaybackCore.createCountdown(3, clock, n => ticks.push([n, clock.now()]));
            await clock.advance(10000);
            const cancelled = [];
            const c = WaybackCore.createCountdown(3, clock, n => cancelled.push(n));
            await clock.advance(1500);
            c.cancel();
            await clock.advance(5000);
            return { ticks, cancelled, timersLeft: clock.pending() };
        """)
        self.assertEqual(result["ticks"], [[3, 0], [2, 1000], [1, 2000], [0, 3000]])
        self.assertEqual(result["cancelled"], [3, 2])
        self.assertEqual(result["timersLeft"], 0)


class TestHistoryLoader(CoreTestCase):
    def test_memo_hit_skips_request(self):
        """(a) Запомненный результат отдаётся сразу, без запроса."""
        result = run_core("""
            const clock = makeClock(), net = makeNet(clock), t = makeDeps(clock, net);
            t.deps.memo.put('example.ru', { kind: 'empty' });
            WaybackCore.createHistoryLoader('example.ru', t.deps).start();
            await clock.advance(0);
            return { kinds: t.kinds(), calls: net.calls.length };
        """)
        self.assertEqual(result, {"kinds": ["empty"], "calls": 0})

    def test_success(self):
        """(b, c) Запрос по контракту, загрузка → успех, результат запомнен."""
        result = run_core("""
            const clock = makeClock(), net = makeNet(clock), t = makeDeps(clock, net);
            WaybackCore.createHistoryLoader('пример.рф', t.deps).start();
            await clock.advance(0);
            const url = net.calls[0].url;
            net.calls[0].respond(200, SNAPSHOT_BODY);
            await clock.advance(0);
            return { url, kinds: t.kinds(), memo: t.deps.memo.get('пример.рф')?.kind, timers: clock.pending() };
        """)
        self.assertEqual(result["url"], "/api/wayback?domain=%D0%BF%D1%80%D0%B8%D0%BC%D0%B5%D1%80.%D1%80%D1%84")
        self.assertEqual(result["kinds"], ["loading", "success"])
        self.assertEqual(result["memo"], "success")
        self.assertEqual(result["timers"], 0)

    def test_pending_retries_after_retry_after(self):
        """(d) 202 → повтор не раньше retry_after, затем успех."""
        result = run_core("""
            const clock = makeClock(), net = makeNet(clock), t = makeDeps(clock, net);
            WaybackCore.createHistoryLoader('example.ru', t.deps).start();
            await clock.advance(1000);
            net.calls[0].respond(202, { status: 'pending', retry_after: 8 }, { 'Retry-After': '8' });
            await clock.advance(7999);
            const callsBefore = net.calls.length;
            await clock.advance(1);
            const retryAt = net.calls[1].at;
            net.calls[1].respond(200, SNAPSHOT_BODY);
            await clock.advance(0);
            return { callsBefore, retryAt, kinds: t.kinds() };
        """)
        self.assertEqual(result, {"callsBefore": 1, "retryAt": 9000, "kinds": ["loading", "success"]})

    def test_deadline(self):
        """(e) Постоянный 202 → таймаут ровно через 2 минуты от start(); повтор после дедлайна не уходит."""
        result = run_core("""
            const clock = makeClock(), net = makeNet(clock), t = makeDeps(clock, net);
            WaybackCore.createHistoryLoader('example.ru', t.deps).start();
            let timeoutAt = null;
            for (let i = 0; i < 300 && timeoutAt === null; i++) {
                for (const c of net.calls) if (!c.done && !c.aborted) c.respond(202, { status: 'pending', retry_after: 30 });
                await clock.advance(500);
                if (t.states.length > 1) timeoutAt = clock.now();
            }
            await clock.advance(60000);
            return { timeoutAt, kinds: t.kinds(), last: t.states.at(-1),
                     calls: net.calls.map(c => c.at), timers: clock.pending() };
        """)
        self.assertEqual(result["timeoutAt"], 120000)
        self.assertEqual(result["kinds"], ["loading", "timeout"])
        self.assertEqual(result["last"], {"kind": "timeout", "reason": "deadline"})
        # Ответ приходит на следующем шаге цикла (+500 мс), повтор — через 30 с после ответа
        self.assertEqual(result["calls"], [0, 30500, 60500, 90500])
        self.assertEqual(result["timers"], 0)

    def test_request_timeout(self):
        """(f) Нет ответа 20 с от отправки → таймаут запроса, запрос прерван."""
        result = run_core("""
            const clock = makeClock(), net = makeNet(clock), t = makeDeps(clock, net);
            WaybackCore.createHistoryLoader('example.ru', t.deps).start();
            await clock.advance(19999);
            const before = t.kinds();
            await clock.advance(1);
            return { before, last: t.states.at(-1), aborted: net.calls[0].signal.aborted };
        """)
        self.assertEqual(result, {"before": ["loading"], "last": {"kind": "timeout", "reason": "request"},
                                  "aborted": True})

    def test_network_error(self):
        """(g) Сетевая ошибка → «не удалось получить историю»."""
        result = run_core("""
            const clock = makeClock(), net = makeNet(clock), t = makeDeps(clock, net);
            WaybackCore.createHistoryLoader('example.ru', t.deps).start();
            await clock.advance(0);
            net.calls[0].fail();
            await clock.advance(0);
            return t.kinds();
        """)
        self.assertEqual(result, ["loading", "failed"])

    def test_cancel_ignores_late_answers(self):
        """(h, i) После cancel() запрос прерван, поздний ответ не показывается, таймеров нет."""
        result = run_core("""
            const clock = makeClock(), net = makeNet(clock), t = makeDeps(clock, net);
            const loader = WaybackCore.createHistoryLoader('example.ru', t.deps);
            loader.start();
            await clock.advance(1000);
            loader.cancel();
            net.calls[0].respond(200, SNAPSHOT_BODY);
            await clock.advance(200000);
            return { kinds: t.kinds(), aborted: net.calls[0].signal.aborted, timers: clock.pending(),
                     memo: t.deps.memo.get('example.ru') ?? null };
        """)
        self.assertEqual(result, {"kinds": ["loading"], "aborted": True, "timers": 0, "memo": None})

    def test_restart_after_error_gets_new_deadline(self):
        """(j) start() после ошибки — новая попытка с новым дедлайном."""
        result = run_core("""
            const clock = makeClock(), net = makeNet(clock), t = makeDeps(clock, net);
            const loader = WaybackCore.createHistoryLoader('example.ru', t.deps);
            loader.start();
            await clock.advance(0);
            net.calls[0].respond(504, { code: 'archive_timeout' });
            await clock.advance(100000);
            loader.start();
            let timeoutAt = null;
            for (let i = 0; i < 400 && timeoutAt === null; i++) {
                for (const c of net.calls) if (!c.done && !c.aborted) c.respond(202, { status: 'pending', retry_after: 7 });
                await clock.advance(500);
                if (t.states.length > 3) timeoutAt = clock.now();
            }
            return { kinds: t.kinds(), first: t.states[1], last: t.states.at(-1), timeoutAt };
        """)
        self.assertEqual(result["kinds"], ["loading", "timeout", "loading", "timeout"])
        self.assertEqual(result["first"], {"kind": "timeout", "reason": "server"})
        self.assertEqual(result["last"], {"kind": "timeout", "reason": "deadline"})
        self.assertEqual(result["timeoutAt"], 220000)

    def test_queue_wait_counts_for_deadline_not_request_timeout(self):
        """(k) Ожидание слота входит в дедлайн, но не в 20-секундный таймаут запроса."""
        result = run_core("""
            const clock = makeClock(), net = makeNet(clock);
            const limiter = WaybackCore.createLimiter(1);
            const a = makeDeps(clock, net, { limiter }), b = makeDeps(clock, net, { limiter });
            WaybackCore.createHistoryLoader('a.ru', a.deps).start();
            WaybackCore.createHistoryLoader('b.ru', b.deps).start();
            await clock.advance(15000);
            const callsWhileBusy = net.calls.length;
            net.calls[0].respond(200, SNAPSHOT_BODY);
            await clock.advance(0);
            const sentAt = net.calls[1].at;
            await clock.advance(19999);
            const bBefore = b.kinds();
            await clock.advance(1);
            return { callsWhileBusy, sentAt, bBefore, bAfter: b.states.at(-1) };
        """)
        self.assertEqual(result["callsWhileBusy"], 1)
        self.assertEqual(result["sentAt"], 15000)
        self.assertEqual(result["bBefore"], ["loading"])
        self.assertEqual(result["bAfter"], {"kind": "timeout", "reason": "request"})

    def test_pending_releases_slot(self):
        """(l) Пока домен ждёт повтора после 202, слот свободен для другого."""
        result = run_core("""
            const clock = makeClock(), net = makeNet(clock);
            const limiter = WaybackCore.createLimiter(1);
            const a = makeDeps(clock, net, { limiter }), b = makeDeps(clock, net, { limiter });
            WaybackCore.createHistoryLoader('a.ru', a.deps).start();
            WaybackCore.createHistoryLoader('b.ru', b.deps).start();
            await clock.advance(1000);
            net.calls[0].respond(202, { status: 'pending', retry_after: 8 });
            await clock.advance(0);
            const second = net.calls[1] && [net.calls[1].url, net.calls[1].at];
            await clock.advance(8000);
            const retryWaiting = net.calls.length;
            net.calls[1].respond(200, SNAPSHOT_BODY);
            await clock.advance(0);
            const third = net.calls[2] && [net.calls[2].url, net.calls[2].at];
            return { second, retryWaiting, third };
        """)
        self.assertEqual(result["second"], ["/api/wayback?domain=b.ru", 1000])
        self.assertEqual(result["retryWaiting"], 2)
        self.assertEqual(result["third"], ["/api/wayback?domain=a.ru", 9000])


class TestTopRowsScenarios(CoreTestCase):
    def test_at_most_four_requests_in_flight(self):
        """(a) Шесть нажатий подряд: в полёте не больше 4 запросов, остальные ждут (FR-019a)."""
        result = run_core("""
            const clock = makeClock(), net = makeNet(clock);
            const limiter = WaybackCore.createLimiter(WaybackCore.MAX_PARALLEL);
            const rows = ['a', 'b', 'c', 'd', 'e', 'f'].map(n => makeDeps(clock, net, { limiter }));
            rows.forEach((t, i) => WaybackCore.createHistoryLoader('d' + i + '.ru', t.deps).start());
            await clock.advance(0);
            const first = net.inFlight();
            net.calls[0].respond(200, SNAPSHOT_BODY);
            await clock.advance(0);
            return { first, afterOne: net.inFlight(), total: net.calls.length,
                     waiting: rows.map(t => t.kinds().at(-1)) };
        """)
        self.assertEqual(result["first"], 4)
        self.assertEqual(result["afterOne"], 4)
        self.assertEqual(result["total"], 5)
        self.assertEqual(result["waiting"], ["success", "loading", "loading", "loading", "loading", "loading"])

    def test_rows_are_independent(self):
        """(b) Ошибка одной строки не меняет состояния других (FR-019)."""
        result = run_core("""
            const clock = makeClock(), net = makeNet(clock);
            const limiter = WaybackCore.createLimiter(4);
            const rows = [0, 1, 2].map(() => makeDeps(clock, net, { limiter }));
            rows.forEach((t, i) => WaybackCore.createHistoryLoader('d' + i + '.ru', t.deps).start());
            await clock.advance(0);
            net.calls[0].respond(200, SNAPSHOT_BODY);
            net.calls[1].respond(200, { snapshot_count: 0, count_truncated: false, first: null, last: null });
            net.calls[2].respond(502, { code: 'archive_error' });
            await clock.advance(0);
            return rows.map(t => t.kinds());
        """)
        self.assertEqual(result, [["loading", "success"], ["loading", "empty"], ["loading", "failed"]])

    def test_cancel_on_list_switch_stops_retries(self):
        """(c) Смена списка: после cancel() повтор по 202 не отправляется, состояния не меняются (FR-019a)."""
        result = run_core("""
            const clock = makeClock(), net = makeNet(clock), t = makeDeps(clock, net);
            const loader = WaybackCore.createHistoryLoader('example.ru', t.deps);
            loader.start();
            await clock.advance(0);
            net.calls[0].respond(202, { status: 'pending', retry_after: 8 });
            await clock.advance(1000);
            loader.cancel();
            await clock.advance(200000);
            return { calls: net.calls.length, kinds: t.kinds(), timers: clock.pending() };
        """)
        self.assertEqual(result, {"calls": 1, "kinds": ["loading"], "timers": 0})

    def test_memo_shared_between_blocks(self):
        """(d) История, полученная в одном блоке, сразу показывается в другом без запроса (FR-015)."""
        result = run_core("""
            const clock = makeClock(), net = makeNet(clock);
            const memo = WaybackCore.createMemo();
            const card = makeDeps(clock, net, { memo }), row = makeDeps(clock, net, { memo });
            WaybackCore.createHistoryLoader('Example.ru', card.deps).start();
            await clock.advance(0);
            net.calls[0].respond(200, SNAPSHOT_BODY);
            await clock.advance(0);
            WaybackCore.createHistoryLoader('example.ru', row.deps).start();
            await clock.advance(0);
            return { row: row.kinds(), calls: net.calls.length };
        """)
        self.assertEqual(result, {"row": ["success"], "calls": 1})


if __name__ == "__main__":
    unittest.main()
