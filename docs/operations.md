# Эксплуатация

## Локальные команды

См. проверяемую последовательность в README. Production-системы автоматически не подключаются. Доставка shadow по умолчанию; reviewed включается явно. В demo получатель — таблица receipts в локальной БД, с собственной транзакцией и unique correlation key.

## Ошибки и восстановление

1. Оператор смотрит `/products/{p}/pipeline` и `/metrics` со своим токеном. Логи содержат только безопасные IDs/stage/error_code, не источник, токены и provider body. HTTP-ошибка содержит request_id и типизированный error.
2. Transient/429/timeout: до 3 попыток, backoff 2^attempt+jitter и Retry-After. Нет вложенных HTTP-retries. Capacity wait не расходует попытки и не запускает model HTTP. Model budget расходуется при реальном старте вызова и не сбрасывается operator retry.
3. После падения worker running lease истекает и захватывается снова. Старый claim не коммитит и не продлевает новый. SIGTERM даёт закончить текущую ограниченную операцию, затем закрывает ресурсы; принудительная остановка восстанавливается через lease. Пустая очередь ждёт 1 сек, source poll — 10 минут.
4. Dead retry: `POST /products/{p}/jobs/{id}/retry`. Только operator; удалённые ревизии и исчерпанный model budget не возрождаются. Повтор не обнуляет ранее использованные вызовы модели. Для новых prompts/новой normalization нужен контролируемый reprocessing с новым version/dedupe key, автоматической массовой переобработки нет.
5. `delivery_unknown`: проверить correlation key в backlog вне агента, зафиксировать результат. Не переотправлять вслепую. Автоматического resolve-endpoint нет: ручную сверку и изменение статуса выполняет доверенный оператор по согласованному runbook компании. До её появления internal delivery с неизвестными capabilities следует оставить shadow.
6. `correction_required`: внешний получатель мог сохранить старый snapshot. Сверить external_id, исправить/отозвать карточку вручную в разрешённой системе. Новая ревизия требует нового решения; старое approval не наследуется. Локальные receipts очищаются от evidence при удалении, но это не гарантия удаления внешних копий.

При массовой недоступности моделей ingest продолжает сохранять данные; ограниченная очередь ждёт capacity/retry, остающиеся dead/partial видимы. Не объявлять success по отсутствию exception в worker loop — проверять pipeline status и coverage.

## Удаление и retention

`support-agent retention atlas` — dry-run; `support-agent retention atlas --execute` — явное удаление с теми же application rules. API operator может dry-run либо поставить retention job в очередь; scheduler не запускает скрытое массовое удаление.

По умолчанию TTL производного контента 90 дней от последнего принятого изменения. Год хранения относится к исходной системе. TTL должен быть утверждён владельцем данных. Удаление/access_revoked не ждёт TTL: tombstone, cancel jobs, removal needs/memberships/cache/raw/segments/quotes/observation dates, invalidation digest/outbox. В карточках очищаются snapshots; минимальные audit events и неперсональные hashes остаются. Tombstone/sequence сохраняются для запрета stale replay. Группы без разрешённых источников теряют канонический текст. Fixtures являются неизменным синтетическим тестовым набором, не хранилищем runtime-вложений; runtime внутреннего режима не сохраняет локальные копии bytes.

После purge проверить: Revision.raw/segments null, Need/MediaCache/Observation отсутствуют, частоты пересчитаны, карточки stale/correction_required. Не строить группы заново из удалённого текста. Неполный тикет не становится полным из-за удаления проблемного evidence.

## Резервное копирование и конфигурация

PostgreSQL и demo receipts — persistent volume `pgdata`; local bootstrap tokens/storage — `agent-storage`. Секреты `.env`, `.local/tokens.json` имеют 0600 и не попадают в git/Docker build context. Для эксплуатации — secret manager компании, отдельная миграционная учётная запись, отсутствие admin URL/секретов в API/worker окружении. В локальном Compose admin URL присутствует для удобства migrate/seed CLI; перед пилотом отделить bootstrap job/secrets от runtime services.

Не удалять volumes для обычного перезапуска. Делать согласованный backup/restore средствами PostgreSQL; при восстановлении внешние записи сверять по correlation key перед возобновлением sender. Для emergency stop остановить sender и scheduler; перевод в shadow и перезапуск исключает новые отправки. Уже начавшийся запрос может завершиться — проверять outbox/unknown/correction.

## Метрики и пределы интерпретации

Coverage = fully processed / все доступные обращения принятого охвата. Partial/dead/needs_review остаются в знаменателе; общий source-wide total неизвестен, API возвращает null. Отдельно видны каналы, очередь, oldest age, p50/p95 processing latency, реальные provider call counts и сообщённые tokens, ASR минуты/VL страницы, retries/invalid JSON, карточки/просмотры/решения, delivery outcomes. IDs не являются метками метрик. Часть отказов авторизации отражена HTTP/audit-контуром, полный security SIEM требует корпоративной интеграции. Нельзя считать закрытие тикета ручными трудозатратами.

Цель пилота — >=95% доступных записей полностью обработаны за 24 часа. Локальный load-test проверяет координацию очереди на mock latency, не Qwen/GPU, полный media profile или достижение SLO. Команда `measure-endpoints` делает 1–5 синтетических вызовов только явно настроенного internal gateway; результат требует отдельной мощности и проверки на реальной смеси каналов.
