# OpenCaseKillers

**[Технический отчёт в PDF: архитектура, технологии и запуск](docs/OpenCaseKillers_Report_RU.pdf)** · [Исходник отчёта](docs/report_ru.md)

Backend-пакет проекта — `support-insight-agent`, команда запуска — `support-agent`.

Работающий MVP по приложенному техническому заданию: обращения поддержки → нормализация текста/вложений → отдельные потребности → повторяющиеся группы одного продукта → карточки с точными числами и evidence → решение владельца → transactional outbox.

Python 3.12+, FastAPI, Pydantic, SQLAlchemy, Alembic, PostgreSQL 17, httpx. Четыре границы сервисов, одна кодовая база. Сложный frontend не требуется: есть CLI, API и OpenAPI.

**Demo полностью синтетический.** Он не вызывает реальные Qwen/ASR/VL и не доказывает качество моделей. Internal подключается только через явно настроенный шлюз, без внешних LLM/API и скрытого fallback. Доставка по умолчанию `shadow`; `reviewed` требует решения уполномоченного reviewer по конкретной revision/hash.

## Быстрый локальный запуск

Нужны Python 3.12+ и работающий Docker Desktop/Engine с Compose. Команды выполнять из корня проекта. Зависимости и Docker-образы скачиваются при установке; обработка demo затем не требует внешней сети.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install uv==0.13.7
.venv/bin/uv sync --frozen --extra dev --cache-dir .cache/uv
python3 scripts/bootstrap.py
docker compose up -d --wait db
.venv/bin/support-agent migrate
DELIVERY_MODE=reviewed .venv/bin/support-agent run-demo
```

На чистой БД: **40 тикетов, 37 участий origin в группах, 6 карточек, 2 локальные доставки**, `repeat_unchanged: true`. В Atlas группа пожеланий CSV имеет 12 независимых origin, дефект экспорта — отдельную группу. Явный дубль считается один раз, независимые одинаковые тексты — отдельно. Плохой ASR и битый PDF видны как needs_review/partial и не добавляют частоту автоматической подборке. В demo-командах часы фиксированы на 6 ноября 2026, 12:00 UTC; обычные процессы используют реальные UTC-часы и бизнес-зону Europe/Moscow.

Повтор команды не создаёт дополнительных карточек/доставок. Отдельный чистый walkthrough проверяет ещё и рестарт, изменение/удаление: CSV origin в Atlas становится 10, удалённый текст/evidence очищается.

```sh
.venv/bin/python scripts/verify_demo.py
.venv/bin/python scripts/start_api.py
```

API: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs). Для API скрипт выбирает отдельную non-owner роль `agent_api`, workers используют `agent_worker`. В другом терминале:

```sh
.venv/bin/python scripts/api_probe.py
```

Токены local bootstrap находятся только в `.local/tokens.json`, хеши — в серверном реестре. `viewer` видит Atlas; `reviewer` принимает решения только по Atlas; `operator` управляет заданиями обоих продуктов без права reviewer; `boreal` согласует Boreal. Не отправляйте `.env` и `.local` вместе с проектом.

## Полный Docker Compose

`build-image.sh` обходит ошибку некоторых Docker Desktop с кириллицей в пути: собирает тот же source через временный ASCII-каталог, исключая secrets/cache. Один образ используют все сервисы.

```sh
python3 scripts/bootstrap.py
sh scripts/build-image.sh
docker compose up -d --wait db
docker compose run --rm --no-deps -T ingest support-agent migrate
docker compose run --rm --no-deps -T ingest support-agent seed-demo
docker compose run --rm --no-deps -T -e DELIVERY_MODE=reviewed ingest support-agent run-demo
docker compose up -d --no-build --wait --wait-timeout 60
docker compose ps
```

Запускаются ingest, media, зарезервированный media_text worker, analysis, delivery API, sender, scheduler и PostgreSQL. API доступен на `127.0.0.1:8000`, PostgreSQL — `127.0.0.1:55432`. Данные переживают restart. Наружу других портов нет. Migrate — одноразовый процесс; scheduler не дублируется внутри API.

При Docker bootstrap токены создаются внутри `agent-storage`; host- и container-файлы токенов — разные источники конфигурации. `seed-demo` в local обновляет серверный реестр по своему credential-файлу. Проверять API из Docker можно без копирования/вывода токенов:

```sh
docker compose exec -T ingest python scripts/api_probe.py --url http://delivery:8000 --tokens /app/.local/tokens.json
```

Если данные уже создавались через host, его `.local/tokens.json` действует до явного Docker seed. Для обычного перезапуска seed не нужен. Compose сохраняет shadow по `.env`; однократный reviewed demo доставляет только локальному получателю. Для интерактивного согласования с доставкой настройте `DELIVERY_MODE=reviewed` одновременно в API/sender и перезапустите их.

## Проверки

```sh
.venv/bin/pytest -q
.venv/bin/ruff check src tests scripts migrations
.venv/bin/support-agent load-test --tickets 3000 --workers 4
.venv/bin/support-agent evaluate
```

Тесты и load-test создают собственные временные PostgreSQL-БД и удаляют только их. SQLite не используется. Нужна локальная миграционная учётная запись с правом CREATE DATABASE; runtime-роли остаются non-owner/NOSUPERUSER/NOBYPASSRLS. Не запускайте тестовые команды с производственной конфигурацией.

Фактические результаты, mapping требований на проверки и непройденные внешние проверки — в [docs/validation.md](docs/validation.md). `evaluate` без независимой разметки и реальных результатов возвращает `not_run`. Даже 100% прохождения fixtures не превращается в precision/recall Qwen.

## Команды и API

```sh
.venv/bin/support-agent ingest-once
.venv/bin/support-agent worker media
.venv/bin/support-agent worker media_text
.venv/bin/support-agent worker analysis
.venv/bin/support-agent worker delivery
.venv/bin/support-agent worker retention
.venv/bin/support-agent scheduler
.venv/bin/support-agent build-digest atlas --period 2026-11-06
.venv/bin/support-agent metrics
.venv/bin/support-agent retention atlas
# Только явный запуск удаления:
.venv/bin/support-agent retention atlas --execute
```

`worker <kind> --once` выполняет ограниченный drain всех готовых этапов, удобный для демонстрации. Для постоянно работающих процессов используйте отдельные команды без `--once`.

| API | Доступ |
|---|---|
| `/health/live`, `/health/ready` | Без контента; readiness проверяет БД/runtime role |
| `GET /products/{p}/groups`, `/cards`, `/cards/{id}` | Разрешённый продукт, пагинация |
| `GET /products/{p}/evidence/{revision_id}` | Только маскированные сегменты своего продукта |
| `GET /products/{p}/digest?period=YYYY-MM-DD` | Проверяемая подборка с числами и источниками |
| `POST /products/{p}/cards/{id}/decision` | product_reviewer; revision/content_hash/decision/reason |
| `GET /products/{p}/pipeline`, `POST .../jobs/{id}/retry` | operator |
| `POST /products/{p}/retention?dry_run=true` | operator; execute ставит контролируемое задание |
| `GET /metrics` | Только operator, без raw content и ID labels |

Для решения JSON: `{"revision":1,"content_hash":"<hash из карточки>","decision":"accepted","reason":"Подтверждено владельцем"}`. Дополнительные поля/поддельные роли отклоняются. Обновление источника инвалидирует старое согласие. Неопределённый результат внешнего создания не переотправляется без идемпотентности/сверки; для уже отправленных устаревших данных есть correction_required.

## Подключение компании

[Контракты шлюза и список внешних зависимостей](docs/integrations.md), [архитектура и инварианты](docs/architecture.md), [retries, deletion, retention, unknown delivery и восстановление](docs/operations.md), [JSON Schema/OpenAPI](docs/schemas/openapi.json).

Нужны реальные endpoints/credentials/CA, issuer и grants, семантика change feed/дублей, возможности backlog, сборки и мощности моделей, правила хранения и независимый размеченный набор. Реальные модели, авторизация компании, media/GPU-нагрузка и 24-часовой SLO пока **not run**. Это локально проверенный MVP, не production-ready система.

Файлы задания использованы как проектный brief; команда из DOCX — контекст. Исходный PDF из iCloud не удалось прочитать из-за системного timeout; отсутствующие фото и review PDF не выдумывались.

## PDF-отчёт

[Скачать технический отчёт](docs/OpenCaseKillers_Report_RU.pdf): сквозной сценарий, схема сервисов, стек, данные и evidence, доступы, команды запуска на хосте и в Docker, API, проверки и условия внутреннего пилота. Исходник — [docs/report_ru.md](docs/report_ru.md).

```sh
.venv/bin/uv sync --frozen --extra dev --extra docs --cache-dir .cache/uv
.venv/bin/python scripts/build_report.py
```

Для сборки нужен DejaVu Sans (например, `fonts-dejavu-core` на Linux); можно передать `--font-dir`. В готовый PDF шрифты встроены. Инструменты отчёта не нужны runtime приложения.

Перед публикацией 8 ноября 2026 полный набор проверен повторно: **42 passed**. Результат — [docs/publication-checks.json](docs/publication-checks.json). Нагрузочные числа в отчёте относятся к сохранённому прогону от 6 ноября.
