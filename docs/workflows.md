# Работа reviewer и оператора

## Проверить потребность

Reviewer читает `/products/{product}/cards` и открывает конкретную карточку. Числа отражают независимые origin в указанном окне; 2–3 источника содержат revision_id, source_version, content_hash и точные маскированные цитаты. Клиент не должен загружать внешние картинки/ссылки из текста как команды.

Для проверки первоисточника используется `/evidence/{revision_id}` с пагинацией `limit/offset`. Доступна только текущая, не удалённая ревизия разрешённого продукта. Частичное покрытие и качество ASR/VL видны явно.

## Разобрать review-очередь

`GET /products/{product}/groups?needs_review=true` показывает группы с provisional/partial/неуверенными потребностями. Детальная группа включает причины review и текущую частоту. Неуверенные и неполные needs не добавляются в автоматическую частоту. Несколько items одного тикета/группы объединяют evidence и причины, а не теряют последующие формулировки.

MVP не даёт интерфейса произвольного редактирования semantic facts. Необходимо исправить/уточнить исходное обращение и принять новую source version. Переобработка завершённой ревизии является no-op; для осознанной смены prompt/model/политики нужен контролируемый новый processing revision, а не бесконечный retry старого результата.

## Принять решение

```json
{"revision": 1, "content_hash": "<hash карточки>", "decision": "accepted", "reason": "Подтверждено владельцем"}
```

POST `/products/{product}/cards/{card_id}/decision`. Допустимы accepted, rejected, needs_clarification. Проверяются серверная роль product_reviewer, scope, revision и hash полного snapshot. Изменение исходников делает прежнее согласие неактуальным.

В shadow решение сохраняется без доставки. В reviewed accepted и outbox создаются одной транзакцией. Получатель в demo — локальный durable receiver; реальная запись требует internal/reviewed, конфигурации и действующих прав.

## Разобрать неизвестную доставку

Оператор читает `GET /products/{product}/outbox` с пагинацией и самостоятельно сверяет correlation key во внешней системе. Endpoint разрешения не выполняет внешнюю сверку за оператора.

POST `/products/{product}/outbox/{outbox_id}/resolve`:

| resolution | Когда использовать | Результат |
|---|---|---|
| delivered + external_id | Получатель подтвердил фактическое создание | delivered, либо correction_required при устаревшем snapshot/approval |
| not_delivered | Надёжно подтверждено отсутствие записи | awaiting_review; отправка остаётся остановленной |
| corrected | Внешняя устаревшая карточка исправлена/отозвана | corrected, с аудитом оператора |

Во всех случаях нужна причина. Подтверждённое отсутствие **не даёт оператору права переотправки**: reviewer должен явно повторить accepted по актуальной карточке; после этого возобновляется тот же correlation key. Если snapshot уже неактуален, нужна новая карточка/ревизия.

CLI использует те же правила:

```sh
.venv/bin/support-agent resolve-delivery atlas OUTBOX_ID --resolution delivered --external-id EXTERNAL_ID --reason "Проверено у получателя"
```

В local/demo по умолчанию используется локальный operator token. Для internal передайте `--token-file /run/secrets/operator-jwt`: подпись, issuer, audience и bindings проверяются как в API. Не помещайте токены в аргументы команд или отчёты.

## Повторить dead job

Оператор вызывает POST `/products/{product}/jobs/{id}/retry`. Старые/удалённые ревизии и исчерпанный model budget не возрождаются. Capacity wait не расходует попытку; validated extraction сохраняется до assignment, поэтому ожидание занятого продукта не вызывает повторный extraction.

Для временного общего сбоя сначала восстановите провайдер/БД, затем проверьте queue/oldest age и потребность в разрешённом retry. Readiness API не доказывает доступность всех моделей: чтение сохранённых карточек может продолжаться при недоступности inference.
