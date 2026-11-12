# Валидация версии 0.2

Актуальная дата — 12 ноября 2026. Результаты текущего прогона записываются в [release-checks.json](release-checks.json). `validation-results.json` от6 ноября и `publication-checks.json` от8 ноября — исторические снимки версии0.1, а не текущая оценка системы.

**Текущий полный прогон: 86 passed, 0 failed; 133,54с.** Wheel, native/Linux walkthrough, API smoke и queue load также прошли. В pytest одно предупреждение Starlette о будущем изменении TestClient; это не ошибка runtime.

## Что запускается

```sh
make check
make check-wheel
make verify
.venv/bin/support-agent load-test --tickets 3000 --workers 4
make docker-demo
make up
```

`make check` объединяет Ruff, формат, schema drift и весь pytest. PostgreSQL — настоящий, не SQLite. Интеграционные тесты создают отдельные временные БД; RLS выполняется non-owner runtime-ролью, concurrency — реальными процессами. В Docker устанавливается обычный wheel, а не editable checkout.

## Проверяемые инварианты

| Область | Сценарии |
|---|---|
| Сквозной путь | intake → media → extraction → grouping → digest → reviewer → durable receiver; restart/replay без дублей |
| Версии | Старый sequence не перезаписывает новый; тот же source_version под новым sequence не создаёт revision/jobs; изменённый immutable payload отклоняется |
| Семантические группы | Mixed items разделены, feature/defect не сливаются, explicit duplicates считают один origin, независимые одинаковые тексты — два |
| Повторы анализа | Extraction хранится до assignment; Capacity не повторяет модель; завершённый/provisional/пустой результат идемпотентен |
| Review и даты | Несколько items одной группы объединяют evidence/review reasons; перефразирование не омолаживает first_seen |
| Подборки | Суточный предел, сохранение valid карточек после invalidation, deterministic count/growth/wait ordering; прошлый период использует сохранённый snapshot |
| Источники | Strict JSON/types/extra fields, неизвестный segment/candidate, выдуманная цитата и чужой product блокируются |
| Очередь | SKIP LOCKED, истёкший claim, fencing, атомарный next enqueue, heartbeat cancellation и освобождение capacity |
| Model limits | Общие slots между процессами, finite retry/Retry-After, общий persisted budget, long-context coverage и bounded matching |
| Доступ | Verified JWT signature/issuer/audience/expiry/algorithm, серверные bindings, API под agent_api, pool context не протекает |
| Доставка | Обрыв после реального создания, idempotency/lookup, malformed success → unknown, revoked reviewer, stale approval, audited resolution и новое reviewer action |
| Privacy | Удаляются historical quotes после смены группы; очищаются Card/Digest/Receipt/review copies/cache/extraction; old worker не воскрешает данные |
| Retention | Hard TTL каждой ревизии, batches/dry-run, сохранение свежего current при очистке старой истории |
| Медиа | Magic/ZIP/macro/path/page/time limits, сохранение успешных файлов, malformed response и partial вместо false complete |
| Настройка | Registry dry-run/idempotence, сохранение cursor, scope validation, запрет удаления активного slot, doctor |
| Миграции | Зафиксированные historical DDL и upgrade/downgrade roundtrip, readiness проверяет head |
| Internal contract | Полный путь через HTTPS с ephemeral CA, настоящая проверка TLS и RS256, HTTP source/models/backlog; без публичного fallback |
| Упаковка | Установка wheel в отдельную среду, CLI из чужой папки, наличие ресурсов, отсутствие секретов и safe Docker context |
| Метрики/evaluator | SQL aggregates без raw/snapshot, operator scope, фиксированные labels; независимый gold denominator не исключает отсутствующие результаты |

## Что означают цифры

Чистый demo ожидает 40 синтетических тикетов, 37 origin/group memberships, 6 карточек и 2 локальные доставки. После update/delete в Atlas CSV остаётся 10 origin вместо12. Плохое распознавание и битый PDF остаются partial/needs_review и не входят в автоматическую частоту.

Queue load на 3000 заданиях проверяет несколько процессов, два продукта, суточный backlog, ограниченную capacity и восстановление после synthetic transient. Его длительность не переводится в Qwen/GPU throughput. Точный результат текущего прогона — в release JSON; число 37,977с относится к предыдущему снимку от6 ноября.

Internal contract использует реальные HTTPS/httpx/JWT/DB механизмы, но управляемые synthetic model responses. Это не запуск корпоративной Qwen. Тесты injection подтверждают конкретные границы workflow; они не доказывают отсутствие любых атак или ошибок декодеров.

Метрики coverage включают partial/dead/needs_review в знаменатель принятого охвата. Полный source-wide denominator неизвестен и помечается null. Неизвестные provider tokens не превращаются в выдуманный расход.

## Непроведённые проверки

**Not run:** реальные корпоративные endpoints/issuer/key rotation, сборки Qwen/ASR/VL, независимая семантическая разметка, real media/GPU поток, endurance и 24-часовой SLO, корпоративный backup/restore с backlog и внешняя security-review.

Цели будущего пилота precision>=90%, recall>=80%, pairwise precision>=90% и >=95% доступных записей за24ч остаются целями. Сценарный набор и математические тесты evaluator не дают фактического качества модели. [Формат независимой оценки](evaluation.md).

Исходный PDF кейса по-прежнему недоступен из-за iCloud timeout. Точный статус материалов и границы вывода — в [source-materials.md](source-materials.md).
