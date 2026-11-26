# Отчёт по Практике 7.2: Поддержка пользователя с помощью AI-инструментов (26 / 16 баллов)

* **Студент:** Владислав Пивцаев (Vyvch)
* **Целевой репозиторий проекта:** [https://github.com/Vyvch/drop_ru_domain_finder](https://github.com/Vyvch/drop_ru_domain_finder)
* **Раздел Issues с результатами классификации:** [https://github.com/Vyvch/drop_ru_domain_finder/issues](https://github.com/Vyvch/drop_ru_domain_finder/issues)
* **Экспорты сценариев n8n:**
  * Sub-workflow классификатора: [`subworkflow_issue_classifier.json`](./subworkflow_issue_classifier.json)
  * Основной workflow триажа: [`workflow_issue_triage.json`](./workflow_issue_triage.json)
  * Workflow еженедельного дайджеста и статистики: [`workflow_weekly_stats.json`](./workflow_weekly_stats.json)
  * Workflow супер-результата (Actor-Critic Auto-Fixer): [`workflow_super_result_autofix.json`](./workflow_super_result_autofix.json)
  * Исходный файл Data Table: [`weekly_stats.csv`](./weekly_stats.csv)

---

## Сводная таблица выполнения критериев (26 / 16 баллов)

| # | Критерий из ТЗ | Баллы | Статус | Подтверждение |
|---|---|:---:|:---:|---|
| 1 | Открытый Issue классифицируется как `bug` / `task` / `feature`, соответствующий лейбл проставлен | **2** | ✅ Выполнено | Issues #28 (`bug`), #29 (`feature`), #30 (`task`), #33 (`bug`) |
| 2 | Issue с `confidence < 0.8` (расплывчатый тикет) получает лейбл `needs-triage`, комментарий НЕ публикуется | **1** | ✅ Выполнено | Issue #31 (`needs-triage`), 0 комментариев |
| 3 | Классификатор вынесен в изолированный Sub-workflow с `Execute Workflow Trigger` и вызывается через `Execute Workflow` node | **2** | ✅ Выполнено | Воркфлоу `Practice 7.2 - Issue Classifier (Sub-workflow)` |
| 4 | Настроен Cron-триггер (`0 9 * * MON`), запросы к GitHub API возвращают актуальные данные за календарную неделю (Пн–Вс) | **2** | ✅ Выполнено | Расчёт границ календарной недели (Пн 00:00 — Вс 23:59 UTC) |
| 5 | Code node агрегирует и группирует статистику за неделю по типам сущностей и по авторам | **2** | ✅ Выполнено | Нода `Group Statistics (Types & Authors)` (автор Vyvch: 25 действий) |
| 6 | Сформировано и отправлено красивое HTML-письмо со сводной таблицей через Gmail node | **2** | ✅ Выполнено | Письмо успешно получено на `vlad.pivtsaev@mail.ru` |
| 7 | Недельная статистика сохраняется в Data Table `weekly_stats` | **1** | ✅ Выполнено | Нода `Save Weekly Stats to Data Table` |
| 8 | В письме отображается дельта (Δ) к показателям прошлой недели из Data Table | **2** | ✅ Выполнено | Колонка `Δ (к прошлой неделе)` в HTML-таблице |
| 9 | В шапке письма выводится блок «AI-комментарий недели», сгенерированный LLM на основе метрик и Δ | **2** | ✅ Выполнено | LLM Chain с моделью GPT-4o-mini сформировала инженерный комментарий |
| ⭐ | **СУПЕР-РЕЗУЛЬТАТ: Полный автономный цикл от баг-репорта до авто-мерджа с Actor-Critic самоисправлением** | **+10** | ✅ Выполнено | Смерджен [PR #34](https://github.com/Vyvch/drop_ru_domain_finder/pull/34), закрыт [Issue #28](https://github.com/Vyvch/drop_ru_domain_finder/issues/28), подтверждена эскалация |
| | **ИТОГО** | **26 / 16** | | **Максимальный балл + Супер-результат** |

---

## 1. Базовая часть: Триаж тикетов и Sub-workflow (Критерии 1–3)

* **Основной воркфлоу:** [`workflow_issue_triage.json`](./workflow_issue_triage.json)
* **Sub-workflow:** [`subworkflow_issue_classifier.json`](./subworkflow_issue_classifier.json)

### Результаты автоматической классификации:
1. **[Issue #28](https://github.com/Vyvch/drop_ru_domain_finder/issues/28)**: *ZeroDivisionError in calculate_domain_score* ➔ классифицирован как `bug`, навешен лейбл `bug`.
2. **[Issue #29](https://github.com/Vyvch/drop_ru_domain_finder/issues/29)**: *Add CSV export endpoint for domain metrics* ➔ классифицирован как `feature`, навешен лейбл `feature`.
3. **[Issue #30](https://github.com/Vyvch/drop_ru_domain_finder/issues/30)**: *Refactor typing annotations and increase pytest coverage* ➔ классифицирован как `task`, навешен лейбл `task`.
4. **[Issue #31](https://github.com/Vyvch/drop_ru_domain_finder/issues/31)**: *Что-то странное происходит* (пустое описание) ➔ уверенность `confidence = 0.5 < 0.8`, навешен лейбл `needs-triage`, 0 комментариев.

![Классифицированные Issues](./screenshots/01_github_issues_classified.png)
![Выполнение Sub-workflow](./screenshots/03_subworkflow_execution.jpg)
![Холст Sub-workflow с парсером](./screenshots/04_subworkflow_canvas.png)

---

## 2. Еженедельный дайджест, Data Table и AI-комментарий (Критерии 4–9)

* **Воркфлоу:** [`workflow_weekly_stats.json`](./workflow_weekly_stats.json)
* **Экспорт таблицы:** [`weekly_stats.csv`](./weekly_stats.csv)

### Ключевые архитектурные решения:
1. **Расчёт календарной недели:** в отличие от скользящего окна, код вычисляет завершённую календарную неделю (с понедельника 00:00:00 по воскресенье 23:59:59 UTC).
2. **Дедупликация:** исключено задвоение связки `issues` и `pull_requests` через проверку по `Set(number)`.
3. **Хранилище состояний:** Data Table `weekly_stats` хранит историю срезов для вычисления дельты ($\Delta$).
4. **AI-комментарий тимлида:** GPT-4o-mini формирует сжатый вывод на основе динамики показателей техдолга и прогресса разработки.

![Полученное HTML-письмо с отчётом](./screenshots/02_weekly_digest_email.png)

---

## 3. ⭐ СУПЕР-РЕЗУЛЬТАТ: Автономный цикл с Actor-Critic и Human Escalation (+10 баллов)

* **Воркфлоу:** [`workflow_super_result_autofix.json`](./workflow_super_result_autofix.json)
* **Реализованный PR с исправлением:** [Pull Request #34 (Merged)](https://github.com/Vyvch/drop_ru_domain_finder/pull/34)
* **Автоматически закрытый баг:** [Issue #28 (Closed)](https://github.com/Vyvch/drop_ru_domain_finder/issues/28)
* **Пример безопасной эскалации:** [Issue #33 (needs-human-review)](https://github.com/Vyvch/drop_ru_domain_finder/issues/33)

### Архитектура цикла:
```mermaid
flowchart TD
    A["GitHub Issues Trigger (label: bug)"] --> B{"Is Bug Label?"}
    B -- No / Escalated --> B_STOP["Skip (Immunity for Human Work)"]
    B -- Yes (Active bug) --> C["Extract Dynamic Issue Data"]
    C --> D["Fetch Repo Tree (GitHub API)"]
    D --> E["AI File Locator (Multi-file)"]
    E --> F["Parse Target Files List"]
    F --> G["Fetch Source Code (GitHub API)"]
    G --> H["Init Self-Correction State (attempt=1, max=3)"]
    
    subgraph Loop["Actor-Critic Self-Correction Loop"]
        H --> I["AI Code Fixer (Generator)"]
        I --> J["Format Candidate Fix"]
        J --> K["AI Judge (Critic / Reviewer)"]
        K --> L["Evaluate Judge Verdict"]
        L --> M{"Judge Approved?"}
    end
    
    M -- "REJECTED (attempt < 3)" --> I
    M -- "REJECTED (attempt >= 3)" --> N["Attempts Limit Reached?"]
    N -- Yes --> O1["Post Escalation Comment (GitHub API)"]
    O1 --> O2["Add Label needs-human-review"]
    O2 --> O_STOP["Safe Halt (No Bad Commits)"]
    
    M -- "APPROVED" --> P["Prepare Approved Patch"]
    P --> Q["Get Main Branch SHA"]
    Q --> R["Create Fix Branch (GitHub API)"]
    R --> S["Commit Fix to Branch (GitHub API)"]
    S --> T["Create Pull Request (Closes #Issue)"]
    T --> U["Auto-Merge PR to Main (Squash)"]
    U --> V["GitHub Auto-Closes Issue"]
```

### Подтверждённые сценарии работы:
1. **Сценарий авто-исправления и слияния (Happy Path):**
   * Баг в `domain_exporter.py` (`ZeroDivisionError` при пустом списке доменов).
   * Локатор точно определил целевые файлы: `domain_exporter.py` и `tests/test_domain_exporter.py`.
   * Генератор добавил защитный код: `if not domains_data: return 0.0`.
   * AI-судья выдал вердикт `APPROVED`: подтверждено устранение первопричины, сохранение типов и сигнатур.
   * Создан и смерджен [PR #34](https://github.com/Vyvch/drop_ru_domain_finder/pull/34).
   * Код на `main` обновлён, все **143 unit-теста проекта успешно пройдены (100% Green)**.
   * [Issue #28](https://github.com/Vyvch/drop_ru_domain_finder/issues/28) автоматически переведён в статус `Closed`.

2. **Сценарий безопасной эскалации (Safe Degradation):**
   * При проверке неверно локализованного файла AI-судья 3 раза подряд отклонил некорректный патч.
   * Воркфлоу **заблокировал слияние в main** и пошёл по ветке эскалации:
     * Опубликовал подробный комментарий в [Issue #33](https://github.com/Vyvch/drop_ru_domain_finder/issues/33) с цитатой AI-судьи.
     * Повесил защитный лейбл `needs-human-review`.
   * При повторных событиях нода `Is Bug Label?` за 5 мс уводит тикет в `False Branch`, исключая вмешательство робота в работу инженеров.

3. **Второй сценарий авто-исправления на новом дефекте (Issue #36 -> PR #37):**
   * Баг в `domain_exporter.py` (`ValueError: dict contains fields not in fieldnames` при экспорте записей с опциональными полями).
   * Multi-file локатор определил целевой модуль и тесты: `domain_exporter.py` и `tests/test_domain_exporter.py`.
   * Генератор реализовал объединение заголовков через `dict.fromkeys(...)` и защитные флаги `DictWriter` (`extrasaction="ignore"`, `restval=""`).
   * AI-судья выдал вердикт `APPROVED`.
   * Создан и автоматически смерджен [PR #37](https://github.com/Vyvch/drop_ru_domain_finder/pull/37).
   * [Issue #36](https://github.com/Vyvch/drop_ru_domain_finder/issues/36) автоматически закрыт.
   * Добавлен регрессионный тест, все unit-тесты проекта (143/143) успешно пройдены.

* **Скриншот безопасной эскалации с комментарием AI-судьи в Issue #33:**
![Эскалация и лейбл needs-human-review](./screenshots/05_super_result_escalation_loop.jpg)

* **Скриншот успешного слияния авто-фикса в PR #37 (Closes #36):**
![Авто-мердж PR 37](./screenshots/07_github_pr_automerged.jpg)

* **Скриншот выполнения воркфлоу в n8n (параллельная обработка и авто-мердж):**
![Выполнение авто-фикса в n8n](./screenshots/06_super_result_n8n_executions.jpg)

---

## 4. Заключение

Практика 7.2 реализована на максимальный возможный балл (**26 из 16 баллов**):
* Полностью автоматизирован триаж, классификация и рассылка метрик с LLM-анализом.
* Построен самокорректирующийся конвейер автономного устранения дефектов с AI-судьёй, защитой от кривых апрувов и автоматическим слиянием в `main`.
* Решение протестировано на живом репозитории с реальными вебхуками и коммитами.
