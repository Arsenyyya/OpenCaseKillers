# Контракт интеграционного шлюза v1

Это спроектированный здесь контракт, **не подтверждённый API Газпром нефти**. Реальные подключения не выполнялись. Названия Qwen 3.8, Qwen-ASR, Qwen-VL воспроизводят задание; сборки, веса, токенизаторы и производительность не подтверждены. Ничего не скачивается автоматически.

## Режимы

`MODE=demo`: конечные fixtures, controlled ASR/VL outputs, детерминированные ответы только для известных входов. Неизвестный вход — permanent error, без имитации понимания. Во время processing внешняя сеть не нужна. Загрузка Python/Docker зависимостей при сборке — отдельный шаг разработчика.

`MODE=internal`, `APP_ENV=internal`: обязательны GATEWAY_URL, GATEWAY_TOKEN, ALLOWED_HOSTS, JWT_ISSUER, JWT_AUDIENCE, JWT_PUBLIC_KEY (PEM-файл). HTTPS с проверкой TLS и опциональным INTERNAL_CA. Нет публичного fallback и verify=False; redirect не выполняется. Базовый URL определяется конфигурацией, content_ref и модельные ответы не назначают URL. HTTP client: timeout 20 секунд, 8 connections, ограниченное тело, никаких собственных retry. Retry делает только очередь; Retry-After учитывается. Local HTTP разрешён только явным test-параметром в local environment.

Все POST с JSON и `Authorization: Bearer <gateway credential>`. Gateway сам проверяет service identity/product scope. Ответы ограничены max_file_bytes. У временных ошибок 429/5xx — retry; остальные 3xx/4xx permanent. Для неизвестного корпоративного формата нужно реализовать адаптер этого шлюза либо заменить узкий Adapter, сохранив контракты.

## Источник

| Endpoint | Вход | Ответ |
|---|---|---|
| `/v1/products/{product}/changes` | `{cursor: string, limit: 1..200}` | `{changes: Change[], next_cursor: string, has_more: bool}` |
| `/v1/products/{product}/tickets/read` | `{ticket_id, version}` | TicketContent точной immutable версии |
| `/v1/products/{product}/attachments/read` | `{content_ref}` | Ограниченные bytes, без redirect |

Change: `{sequence: int>0, external_id, version: opaque string, operation: upsert|delete|access_revoked, changed_at: ISO UTC, backfill: bool}`. sequence строго возрастает в рамках source+product. Batch отсортирован, cursor стабилен, повторяется после сбоя, страница никогда не пропускает подтверждённые изменения. Удаление и отзыв доступа — tombstone с более новым sequence. Версия снимка может быть ETag, но сравнение порядка идёт по sequence. Если источник не имеет такого feed, шлюз должен перечитать актуальное состояние и надёжно разрешить гонки до подтверждения Change; существующий адаптер не обещает порядок для неупорядоченного источника.

TicketContent: `{external_id, version, text, channel: text|audio|document|image, occurred_at: ISO timestamp with timezone, attachments: Attachment[], duplicate_of: external_id|null}`. Комментарии включаются в text, связанные звонки либо attachment, либо явный duplicate_of. Корни дублей передаются раньше ссылок. Изменение duplicate_of пересчитывает origin потомков в той же транзакции; циклы отклоняются без продвижения курсора. Предохранители: до 1000 предков и 10 000 потомков одного изменения. Более крупный граф требует нормализации/разбиения шлюзом. occurred_at относится к исходному запросу/потребности; время выгрузки не подставляется как будто это дата возникновения.

Attachment: `{id, content_ref, declared_type, transcript?: string, transcript_quality: usable|poor|unknown}`. Фактический magic определяет decoder. Поддержаны PNG/JPEG, PDF, DOCX без macro, PCM WAV. ZIP не обрабатывается как произвольный архив. Максимум 8 файлов/тикет, 10 MB/файл, 30 PDF-страниц, 20 MP/изображение, 20 минут аудио по умолчанию. Точные ограничения конфигурируются. Gateway отвечает за безопасный media decoder ASR/VL; PDF page request означает обработку только выбранной страницы.

## Модели

| Endpoint | Поля запроса | Ответ |
|---|---|---|
| `/v1/models/extract` | model, system, prompt_version, schema, segments[], repair, max_output_tokens=3000 | `{output_json: string, usage?: {input_tokens?: int, output_tokens?: int}}` |
| `/v1/models/match` | model, system, prompt_version, schema, item, candidates[], segments[], max_output_tokens=800 | тот же формат |
| `/v1/models/media` | model, data_base64, media_type: audio|image|pdf_page, page: int|null | `[{text, quality?: usable|poor|unknown, confidence?: number, start_ms?: int, end_ms?: int}]` |

Media timestamps для WAV должны лежать в реальной длительности. Нет confidence → null/unknown, не искусственный процент. ASR подтверждает текст распознавания. Quality unknown/poor даёт неполное покрытие и исключение из автоподборки. Поскольку base64 увеличивает HTTP payload, конфигурация шлюза должна согласовать лимит decoded file и JSON body. Генерируемые JSON Schema лежат в `docs/schemas/`.

## Backlog

* `/v1/backlog/cards`: `{product_id, correlation_key, card}` → `{external_id: string}`. При BACKLOG_IDEMPOTENT=true заголовок `Idempotency-Key` обязан атомарно предотвращать второе создание даже при обрыве ответа.
* `/v1/backlog/lookup`: `{product_id,key}` → `{external_id: string|null}` при BACKLOG_LOOKUP=true. Поиск должен быть строгим, по correlation key, с гарантией видимости завершённой записи перед разрешением повторного POST.
* Если нет обеих возможностей, неопределённый исход становится delivery_unknown, автоматический повтор остановлен.
* Автоматический edit/revoke не заявлен: correction_required требует ручной сверки и коррекции уполномоченным владельцем. Уже отправленные внешние копии нельзя удалить транзакцией нашей БД.

## Что получить до корпоративного пилота

Подтверждённые endpoints/форматы и service credentials; надёжный change feed и семантику удаления/дублей; реестр продуктов и владельцев; публичные ключи issuer и процедуру ротации; внутренний CA; правила доступа к исходникам и срокам хранения; гарантии идемпотентности/lookup/correction backlog; сборки и capacity моделей, реальные ограничения context/output; обезличенную независимую разметку и исходный профиль нагрузки. Связь с known backlog пока None: подтверждённого справочника задач нет. Нельзя выдавать demo-доставку и сценарные тесты за проверку этих зависимостей.
