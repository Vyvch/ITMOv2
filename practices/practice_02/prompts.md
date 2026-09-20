# Журнал экспериментов Практики 2

Файл ведёт OpenCode по вашим запросам. Агент записывает фактические результаты экспериментов и вносит изменения в связанные файлы. Свою оценку сообщайте ему в чате; вручную заполнять шаблон не нужно.

- Выбранный слабый артефакт Практики 1:
- Что в нём нужно улучшить:
- Как поймём, что изменение полезно:

| Техника | Файл эксперимента | Изменённый файл Практики 1 | Конкретное изменение | Проверка | Что отклонили |
|---|---|---|---|---|---|
| Few-shot | [`few_shot/experiment.md`](few_shot/experiment.md) | [`tests_integration.md`](../practice_01/tests_integration.md) | Заменили абстрактные тесты на конкретные сценарии (с pytest) | Студент одобрил промпт | Ничего |
| R.C.T.F. | [`rctf/experiment.md`](rctf/experiment.md) | [`tests_integration.md`](../practice_01/tests_integration.md) | Добавлен строгий Definition of Done для тестов | Согласовано | - |
| Chain of Verification | [`chain_of_verification/experiment.md`](chain_of_verification/experiment.md) | [`tests_integration.md`](../practice_01/tests_integration.md) | Добавлен сценарий тестирования безопасности (HMAC) | Согласовано | Тест на Rate Limit (429) отклонён моделью |
| Tree of Thoughts | [`tree_of_thoughts/experiment.md`](tree_of_thoughts/experiment.md) | [`tests_integration.md`](../practice_01/tests_integration.md) | Добавлено архитектурное обоснование выбора инструмента мокинга | Согласовано | Альтернативы (DI и Docker) отброшены |
| RAG | [`rag/experiment.md`](rag/experiment.md) | [`tests_integration.md`](../practice_01/tests_integration.md) | Добавлен точный тест на GitHub Rate Limit на основе документации | Согласовано | - |
| ReAct | [`react/experiment.md`](react/experiment.md) | [`tests_integration.md`](../practice_01/tests_integration.md) | Добавлено Executive Summary после аудита структуры | Согласовано | - |
