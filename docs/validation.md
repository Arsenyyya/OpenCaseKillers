# Фактическая проверка

Дата: 6 ноября 2026, Europe/Moscow. Проверка локального MVP; production-ready не заявляется.

## Окружение и результаты

| Проверка | Фактический результат |
|---|---|
| Python / PostgreSQL | Host Python 3.12.7, Linux image Python 3.12.16; настоящий PostgreSQL 17.6 в Docker |
| Полный pytest | **42 passed**, 101.24 s; без SQLite и без пропущенных тестов |
| Дополнительный финальный e2e | 7 passed, 41.57 s; затем полный финальный набор снова прошёл целиком, включая hash полного snapshot |
| Статический анализ | `ruff check src tests scripts migrations` проходит; форматирование применяется Ruff |
| Чистое demo, host | 40 тикетов, 37 origin/group memberships, 6 карточек, 2 локальные доставки |
| Чистое demo, Linux Docker | Те же числа; декодеры работают в дочерних процессах с RLIMIT_AS 768 MiB/CPU/wall limits |
| Restart + полный повтор | Числа не меняются, дополнительной доставки нет |
| Update/delete | Atlas CSV: 12 → 10 origin; raw/segments удалённой ревизии очищены; новая карточка требует нового решения |
| Compose | Образ собран; все сервисы подняты; PostgreSQL и delivery API healthy; остальные процессы running |
| Реальный локальный HTTP API | readiness 200, разрешённые карточки доступны; чужой продукт и anonymous metrics — 403 |
| Queue load | **3000/3000 succeeded**, 4 процесса, **37.977 s**, peak provider calls **2** при лимите **2** |
| Временная недоступность | В load-профиле по одному transient сбою на каждый из четырёх процессов; все задания восстановлены |

В pytest есть предупреждение библиотеки Starlette о будущем переходе TestClient с httpx на httpx2. Оно не является падением. Runtime HTTP-клиент намеренно использует требуемый заданием httpx.

Промежуточные прогоны обнаруживали ошибки интеграции, в том числе права local service role, исключение decoder для битого PDF, импорт нового теста и гонку smoke-запроса с ещё не готовым API. Они исправлены; для startup smoke теперь используется `compose up --wait`. Итоговые результаты указаны выше. Обычный `docker compose build` на этом Docker Desktop упал из-за Unicode path в BuildKit; проверенный `scripts/build-image.sh` собирает образ через временный ASCII-путь.

## Содержательные инварианты

| № из задания | Проверяемое поведение | Тесты |
|---|---|---|
| 1 | Полный путь, approval, durable receiver, новый Application после рестарта | `e2e/test_pipeline.py::test_full_path_restart_replay_and_counts`; `scripts/verify_demo.py` |
| 2 | Replay batch и повтор analysis сохраняют числа, карточки/revisions | `test_cursor_batch_transaction_and_replay`, `test_reprocess_same_revision_is_material_noop` |
| 3 | Одинаковые независимые тексты считаются отдельно, explicit duplicate один раз | Полный e2e; `test_duplicate_graph_updates_recount_descendants_and_reject_cycles` |
| 4 | Mixed извлекается двумя items, feature/defect разделены | Полный e2e и строгие контракты |
| 5 | Два реальных процесса, один lease; старый fencing token отвергается | `integration/test_concurrency.py::test_two_process_claim_and_fencing_after_expiry` |
| 6 | Конкурентное assignment одного продукта не создаёт две CSV-группы; другой продукт получает свой lease | `test_concurrent_grouping_no_conflicts_and_product_fairness` |
| 7 | Старый ordered event не перезаписывает новую opaque version; tombstone побеждает old claim | `test_update_delete_cancel_approval_purge_and_no_resurrection` |
| 8 | Изменение атомарно заменяет участия; повтор не омолаживает first_seen | Update/delete, replay/no-op e2e; Observation хранит стабильное время |
| 9 | Чужие ID/product/evidence/candidate, роль из body/header запрещены | `integration/test_security.py`; `unit/test_contracts.py` |
| 10 | Forced RLS под non-owner runtime ролью, scope не протекает через pool | `test_forced_rls_runtime_role_empty_scope_and_pool_reset`; API отдельно под agent_api |
| 11 | Невалидный JSON ремонтируется один раз; плохая цитата не записывается; model budget конечен | `test_invalid_model_output_repairs_once_then_dead_others_continue`, `test_persisted_model_budget_stops_retries_and_capacity_returns_slot` |
| 12 | Text/image/audio injection fixtures проходят ограниченные schemas; workflow не выдаёт модели write-tools или произвольный URL | Полный e2e, API spoofing, strict extra/candidate/evidence, gateway arbitrary URL rejection |
| 13 | 429/503/transient/permanent, Retry-After, максимум 3 попытки, прочие jobs продолжают работу | `integration/test_gateway.py`, `integration/test_failures.py` |
| 14 | Crash между записью stage result и enqueue откатывает оба изменения | `test_stage_completion_and_next_enqueue_are_atomic`; intake atomicity |
| 15 | Обрыв после фактического создания: correlation lookup/idempotency; без capabilities — unknown | HTTP gateway tests; `test_timeout_after_local_delivery_does_not_duplicate` |
| 16 | Старое approval отменено новой source revision; revoked reviewer больше не разрешает доставку | Update/delete e2e; `test_reviewer_revocation_blocks_pending_delivery` |
| 17 | Удаление чистит raw/evidence/cache/observations/частоты; old worker не воскрешает | Update/delete e2e; `test_retention_dry_run_and_verified_purge` |
| 18 | Реальный magic, page/ZIP/macro/path limits, плохой файл partial; decoder time/memory isolation | `test_media_type_zip_limits_paths_and_pdf_text_layer`; broken fixture e2e; Linux walkthrough |
| 19 | Общие slots в независимых процессах; round robin продуктов, fresh > backfill; зарезервированный text worker | `test_provider_slots_are_shared_between_processes`, `test_backfill_priority_and_round_robin`, `test_reserved_text_queue_progresses_while_media_is_leased` |
| 20 | Ошибки/логи не отражают secret payload, обычная выдача маскируется, metrics закрыты | API security test; invalid output/retry tests с caplog |

Дополнительно: RS256 signature/issuer/audience/expiry/algorithm проверяются на локально сгенерированном ключе; неподписанные/чужие JWT не дают прав. Суточный предел 5 сохраняется между scheduler-проходами; удаление одной карточки оставляет остальные valid references. Длинный текст обрабатывается до последнего сегмента, а matching получает только ограниченные evidence, не весь длинный тикет.

Тесты prompt injection проверяют архитектурные границы на синтетических providers. Они **не** доказывают устойчивость реальной Qwen ко всем атакам. Точные цитаты доказывают происхождение текста, а не безошибочность интерпретации/ASR. Нормальные и лимитные fixture-тесты не являются fuzzing всех декодеров.

## Воспроизводимые команды

Из корня после bootstrap, поднятой БД и миграций:

```sh
.venv/bin/pytest -q
.venv/bin/pytest tests/e2e -q
.venv/bin/ruff check src tests scripts migrations
.venv/bin/python scripts/verify_demo.py
sh scripts/build-image.sh
docker compose run --rm --no-deps -T ingest python scripts/verify_demo.py
docker compose up -d --no-build --wait --wait-timeout 60
.venv/bin/python scripts/api_probe.py
.venv/bin/support-agent load-test --tickets 3000 --workers 4
.venv/bin/support-agent evaluate
```

Host API probe использует host `.local/tokens.json`. Если серверный реестр явно обновлялся Docker seed, использовать контейнерный probe с его credential-файлом, как описано в README. Токены в отчёты не сохраняются.

## Not run и границы вывода

* Реальные Qwen 3.8 / Qwen-ASR / Qwen-VL, качество extraction/grouping/retrieval на независимой разметке: **not run**. Цели будущего пилота precision >=90%, recall >=80%, pairwise precision >=90% — цели, не результаты.
* Корпоративные endpoints, реальный issuer/grants/CA, права service identities, полнота/порядок corporate feed, граф реальных дублей и capabilities backlog: **not run**. Подтверждённого API компании не предоставлено.
* 1000 обращений/сутки, 20% звонков по 5 минут, 100 изображений/страниц — иллюстративный профиль. Выполнена queue/slot часть на 3000 заданиях, двух продуктах, суточном backlog и mock latency. Полный media/GPU профиль, реальная суточная свежесть >=95% за 24 часа, длительный endurance и реальные размеры файлов: **not run**.
* Абсолютное число доступных source-wide обращений неизвестно. Coverage в API относится ко всему принятому охвату, partial/dead не исключаются из знаменателя. Демонстрация не измеряет трудозатраты поддержки или денежную экономию.
* Backlog edit/revoke capabilities неизвестны: correction_required предполагает контролируемую ручную коррекцию. Общей транзакции PostgreSQL + внешняя система нет; exactly-once не заявляется.
* Ротация JWT keys, secret-manager deployment, отдельные stage DB identities, резервное восстановление с реальным backlog, внешняя security-review и полная DLP требуют работы в корпоративном контуре.
* Исходный PDF в iCloud дважды вернул системный timeout даже при прямом разрешённом чтении. Использованы доступный технический brief и DOCX; фото и дополнительный review PDF отсутствовали. Их содержание не выдумывалось.

`support-agent evaluate --gold FILE --results FILE` принимает независимые JSON: tickets с уникальными ticket_id/channel и items с человеческими need_id/kind, groups со списками need-occurrence IDs; результаты также содержат candidate_ids/gold_candidate_id, complete и evidence_valid. Для real results обязательны `provider=internal`, отсутствие synthetic. Результат считает extraction PR, pairwise PR, candidate recall, evidence validity и coverage каналов. Сопоставление need_id должно быть выполнено независимой разметкой; точное совпадение произвольных модельных пересказов не выдаётся за семантическую метрику.
