# Изменения

## 0.2.0 — 12 ноября 2026

Завершение локального MVP по доступному техническому заданию.

- Provenance-aware purge удаляет цитаты из исторических карточек, дайджестов и receipts даже после смены группы.
- Retention применяет hard TTL к каждой ревизии; ограниченные batches не удаляют свежую текущую ревизию вместе со старой историей.
- Повтор одной source version под новым sequence не создаёт принятую ревизию или jobs; изменение содержимого без смены version отклоняется.
- Extraction сохраняется перед assignment; Capacity/retry не сжигают бюджет повторными вызовами.
- Review причины/evidence объединяются; GroupObservation сохраняет дату потребности при перефразировании в той же группе.
- Исправлены fencing при release и cleanup/отмена при потере heartbeat.
- Неизвестные доставки разрешаются оператором с аудитом; подтверждённое отсутствие требует нового решения reviewer.
- Добавлены provision registry, doctor, ограниченный trusted task catalog и начальный source window.
- Миграции заморожены; runtime_READY проверяет установленную Alembic head, права и non-owner роль.
- API получает review/outbox/detail endpoints, ограничение размера, пагинацию evidence и счётчики отказов.
- Метрики считаются агрегатами PostgreSQL без загрузки raw content и snapshot JSON в процесс API.
- Wheel включает runtime-ресурсы и проверяется вне checkout; Docker использует non-editable установку.
- Build context исключает env variants, private keys, token files и symlinks; runtime не получает admin URL.
- Добавлены Makefile, GitHub CI, проверка schema drift, обновлённая документация и PDF.

## 0.1.0 — 8 ноября 2026

Первый опубликованный demo pipeline, 42 теста, локальная PostgreSQL-очередь, RLS, согласование/outbox и технический PDF. Результаты этой версии сохранены как исторические snapshots.
