# Эксплуатация и восстановление

## Окружения и credentials

Административная учётная запись используется только для `migrate`, `provision` и local demo bootstrap. В Compose это одноразовые `migrate` и tools-service `bootstrap`. В среду API/workers не передаются DATABASE_ADMIN_URL, POSTGRES_PASSWORD, WORKER_PASSWORD или API_PASSWORD как отдельные переменные; runtime получает только свой non-owner DATABASE_URL и необходимые параметры шлюза/issuer.

Host `.env` — удобство локальной разработки, а не production secret distribution. Для компании нужны отдельные окружения административного job и runtime, read-only mounts CA/public key и её secret manager. `.env`, env variants, токены, private keys и caches исключены из Git/build context/wheel.

`support-agent doctor` проверяет runtime-роль, актуальность Alembic head, configured products/cursors/assignment leases, provider slots и demo corpus. Он не выводит credentials и не обращается к реальным моделям по умолчанию. Internal connectivity помечается not_probed.

## Очередь

Jobs переходят queued → running → succeeded, retry_wait, dead или cancelled. Claim — короткая PostgreSQL-транзакция с SKIP LOCKED и уникальным token. Сеть/модель/декодирование выполняются вне DB-транзакции. Heartbeat продлевает job, assignment и slot; потеря владения отменяет полезную работу. Просроченный worker не коммитит результат нового владельца.

Временная ошибка/429/timeout повторяется максимум трижды, с exponential backoff+jitter и Retry-After. Сам httpx retry не делает. Capacity wait не расходует попытки; extraction сохраняется до ожидания assignment и не вызывается заново при каждом ожидании. Model budget считается по всей принятой ревизии, включая неудачные обращения.

Оператор смотрит `/pipeline`, `/outbox` и `/metrics` своего scope. Разрешённый retry dead job не обнуляет model budget, не восстанавливает удалённый контент и не обходит текущую accepted revision. При исчерпанном бюджете сохраняется явная partial/coverage failure, а job остаётся dead.

`worker <kind> --once` обрабатывает только выбранный stage. `scripts/verify_demo.py`/`run-demo` используют общий bounded drain всех этапов. Пустая очередь ждёт, SIGTERM завершает ограниченную текущую работу и закрывает ресурсы; аварийная остановка восстанавливается через lease.

## Неопределённая доставка

1. Остановить автоматические повторения этого outbox; delivery_unknown уже не переотправляется вслепую.
2. Проверить correlation key у получателя.
3. Под ролью operator зафиксировать delivered, not_delivered или corrected через `/outbox/{id}/resolve` либо `resolve-delivery` CLI.
4. При not_delivered требуется новое явное accepted от reviewer. Оператор не получает права согласования.
5. При устаревшем отправленном snapshot выполнить контролируемую внешнюю коррекцию; подтвердить corrected только после неё.

Причина, actor и время сверки сохраняются. Свободный текст маскируется; при обязательном удалении связанных данных его копии очищаются. См. [точные запросы](workflows.md). Никакой общей транзакции PostgreSQL + внешний backlog нет, exactly-once не обещается.

## Удаление и hard retention

Удаление/access_revoked создаёт tombstone, отменяет jobs и очищает все ревизии тикета: raw, segments, validated extraction, media cache, needs, observation dates. По структурным ticket_id/revision_id очищаются также старые карточки, дайджесты, review comments и local receipts — даже если тикет давно перешёл в другую группу. Частоты пересчитываются; pending outbox отменяется, отправленный помечается для коррекции.

TTL по умолчанию 90 дней применяется к **каждой Revision.created_at**, а не к последнему обновлению тикета. Свежая текущая ревизия не удаляется из-за истечения старой истории. Если истекла текущая, тикет tombstone-ится. Год хранения относится к исходной системе и не даёт бессрочного хранения производных данных.

```sh
# Preview: metadata only, default batch100, maximum500:
.venv/bin/support-agent retention atlas --limit 100
# Следующая страница preview:
.venv/bin/support-agent retention atlas --limit 100 --offset 100
# Явное ограниченное удаление; execute всегда offset0:
.venv/bin/support-agent retention atlas --execute --limit 100
```

Вывод содержит revision_ids, ticket_ids, total_revisions и has_more. Повторяйте execute с offset0, пока has_more не станет false. API execute ставит задание; отдельный Compose-сервис retention (worker retention) продолжает ограниченные batches отдельными jobs. Scheduler не запускает скрытое массовое удаление. Минимальный audit и безопасные ordering/tombstone metadata сохраняются; семантические группы не восстанавливаются из удалённого текста.

## Метрики

Метрики агрегируются PostgreSQL: API не загружает raw content, большие сегменты и Card.snapshot ради подсчёта. Scope и доступ operator проверяются сервером. Ticket/user IDs не являются labels.

Coverage = fully processed / весь доступный принятый охват; partial/dead/needs_review остаются в знаменателе. Source-wide total неизвестен и возвращается null, пока шлюз не предоставляет соответствующую статистику. Времена обработки, доставки и решения различаются. Card latency отсчитывается от первого создания стабильной карточки; это не трудозатраты и не длительность закрытия тикета.

Provider calls — попытки запуска адаптера, включая ошибки; токены суммируются только при сообщении провайдером. Синтетический benchmark не измеряет реальное число GPU-часов. Рост очереди, oldest age, retry rate, invalid JSON, отказанные действия и delivery statuses видимы отдельно.

## Обновление и резервирование

Перед переходом 0.1 → 0.2 остановите локальные workers/API, сохраните backup PostgreSQL и примените миграции отдельной учётной записью. Предыдущие миграции заморожены; 0004 добавляет новые constraints/поля и ограничивает runtime grants. Readiness откажет старой схеме вместо тихого запуска с отсутствующими колонками.

Не удаляйте volumes при обычном restart. Восстановление реальной БД с внешним backlog требует проверки correlation keys до возобновления sender. Аварийное отключение: остановить sender/scheduler, проверить уже начатые запросы, перевести API/sender в shadow и перезапустить. Завершившийся внешний HTTP-запрос нельзя отменить одной сменой env-переменной.
