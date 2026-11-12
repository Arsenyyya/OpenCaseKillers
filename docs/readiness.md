# Состояние реализации по заданию

Актуальная версия — 0.2.0. «Проверено» ниже означает локальные автоматические сценарии на настоящем PostgreSQL и тестовом шлюзе; оно не заменяет корпоративный пилот.

| Область | Доступный результат | Внешняя зависимость |
|---|---|---|
| Источник и ревизии | JSONL/HTTP, stable cursor, ordered sequence, immutable source version, 30d initial_since, атомарный enqueue | Реальные поля/семантика change feed компании |
| Контент | Текст, DOCX, текстовый/смешанный PDF, PNG/JPEG, PCM WAV; bounded decoder processes | Реальные ASR/VL и поддержка иных форматов шлюзом |
| Extraction | Strict schema, bounded context/calls, exact evidence, stored extraction | Качество Qwen на независимом наборе |
| Группировка | Product/kind scope, FTS/trigram top-k, review, origin count, stable first_seen | Настройка словаря/порогов на реальном профиле |
| Подборки | До5 разных новых/изменившихся карточек в текущий business-day, точные числа и источники | Согласование приоритетов владельцами |
| Решения и запись | Revision/hash approval, outbox, capability-aware delivery, reconciliation и reapproval | Идемпотентность/lookup/correction реального backlog |
| Доступ | Verified JWT/local tokens, grants, FORCE RLS, non-owner roles, safe scope | Issuer/key rotation и service identities компании |
| Privacy | Provenance purge всех копий, per-revision hard TTL, bounded dry-run/delete | Утверждённая retention/DLP политика и внешнее удаление |
| Надёжность | SKIP LOCKED, fencing, heartbeat cancellation, bounded retries, shared slots | Реальная capacity моделей и DB pool sizing |
| Настройка | Provision registry/known tasks, doctor, external configuration | Реальные sub/products/owners/CA/credentials |
| Упаковка | Installed wheel, packaged resources, Docker, Makefile, CI, schema drift | Окружение эксплуатации и её secret manager |
| Проверка | Unit/integration/e2e/concurrency, HTTPS+JWT internal contract, wheel smoke | Независимые real-model метрики и endurance/GPU профиль |

## Граница завершённости

Доступный локальный контур должен воспроизводиться по README, включая согласование/доставку, повтор без дублей, update/delete и очистку старой истории. Результаты запуска фиксируются в [валидации](validation.md) и `release-checks.json`.

Остаются **not run**: реальные Qwen/ASR/VL, company endpoints/issuer, качество extraction/retrieval/grouping на независимой разметке, real media/GPU throughput, корпоративные backup/restore и security review. Цели пилота precision >=90%, recall >=80%, pairwise precision >=90% и >=95% доступных записей за24ч — цели, а не полученные числа.

MVP не включает произвольное редактирование/слияние потребностей человеком, дообучение, универсальную платформу агентов или автоматическое исправление продукта. Эти функции не требуются выбранным техническим brief. Приоритет срочных инцидентов остаётся у штатной поддержки.
