# OpenCaseKillers

[![Verify project](https://github.com/Arsenyyya/OpenCaseKillers/actions/workflows/verify.yml/badge.svg)](https://github.com/Arsenyyya/OpenCaseKillers/actions/workflows/verify.yml)

Аналитический backend для продуктовых команд: **обращения поддержки → проверяемые потребности → повторяющиеся группы → согласованные карточки → backlog**.

Модель интерпретирует содержание. Код проверяет источники, права, версии, частоты и допустимость записи. Пожелания, дефекты, сервисные запросы и инциденты разделяются; продуктовый reviewer принимает окончательное решение.

**Версия 0.2.0 · 86 тестов прошли.** Локальный контур работает без внешней сети во время обработки. Demo использует явно синтетические данные и конечный набор ответов провайдеров. Качество реальных Qwen и готовность интеграции с компанией не заявляются.

[PDF-отчёт](docs/OpenCaseKillers_Report_RU.pdf) · [Документация](docs/README.md) · [Состояние требований](docs/readiness.md) · [Результаты проверок](docs/validation.md)

## Быстрый старт

Нужны Python 3.12+, Docker с Compose и `make`. Из корня проекта:

```sh
make demo
make doctor
make api
```

`make demo` устанавливает зафиксированные зависимости, создаёт локальные credentials, поднимает PostgreSQL, применяет миграции и запускает reviewed demo. `make api` запускает API под отдельной ограниченной ролью БД.

**На чистой БД:** 40 синтетических тикетов → 37 участий origin в группах → 6 карточек → 2 локальные доставки; повтор не создаёт дублей. Отдельная проверка рестарта, изменений и удаления:

```sh
make verify
```

OpenAPI: **http://127.0.0.1:8000/docs**. В другом терминале можно выполнить `.venv/bin/python scripts/api_probe.py`. Если порт уже занят Docker API, используйте существующий процесс либо `scripts/start_api.py --port 8001`.

Демо-данные зафиксированы на 6 ноября 2026 для воспроизводимости. Постоянные процессы используют текущие UTC-часы; бизнес-календарь — Europe/Moscow. [Подробный запуск без make и работа с токенами](docs/quickstart.md).

## Основные команды

| Задача | Команда |
|---|---|
| Список команд | `make help` |
| Полная проверка: lint, формат, schemas, PostgreSQL tests | `make check` |
| Проверить установленный wheel вне checkout | `make check-wheel` |
| Собрать образ и пройти demo в Docker | `make docker-demo` |
| Запустить / остановить сервисы, сохранив данные | `make up` / `make stop` |
| Пересоздать JSON Schema и OpenAPI | `make schemas` |
| Собрать PDF | `make report` |
| Проверить окружение, миграции, registry и capacity | `make doctor` |

Для полного Docker-пути сначала выполните `make docker-demo`, затем `make up`. Привилегированные `migrate`/`bootstrap` отделены от runtime; API и workers не получают admin URL/пароль владельца БД. [Docker и подробные команды](docs/quickstart.md#docker).

## Структура проекта

```text
src/support_agent/
  services/             ingest, media, analysis, delivery и общие правила состояния
  adapters/             источник, модели, backlog и ограниченное storage
  contracts/ auth/ db/   строгие схемы, доступ, данные и транзакции
  queue.py              leases, fencing, retry и shared provider slots
  provisioning.py       административный реестр продуктов и issuer-subjects
  diagnostics.py        безопасный doctor без вывода credentials
  observability/        агрегированные метрики без исходного контента
prompts/ config/        версионируемые prompts и конфигурационные примеры
migrations/            зафиксированная история схемы и RLS
fixtures/demo/          синтетические обращения, медиа и controlled responses
tests/                  unit, integration, e2e и packaging
scripts/ deploy/        проверки, сборка, PDF и развёртывание
docs/                   сценарии работы, архитектура, контракты и ограничения
```

Python-пакет называется `support-insight-agent`, CLI — `support-agent`. Wheel содержит runtime-ресурсы, prompts, demo и миграции и работает вне исходного репозитория. Docker устанавливает такой же non-editable wheel.

## Что завершено в 0.2

Исправлены удаление исторических цитат, hard TTL каждой ревизии, повторы extraction при занятом assignment lease, повторная синхронизация одной source version, восстановление неизвестной доставки и ручная очередь review. Добавлены административный `provision`, `doctor`, trusted backlog catalog, сохранённые схемы, переносимая упаковка, безопасный build context и CI.

[Изменения версии](CHANGELOG.md) · [Проверяемые сценарии](docs/validation.md) · [Работа оператора и reviewer](docs/workflows.md)

## Подключение компании

Для внутреннего режима нужен подтверждённый HTTPS-шлюз, credentials/CA, корпоративный issuer с проверяемым RSA-ключом, серверные привязки продуктов и пользователей, гарантии change feed/backlog и реальные мощности моделей. Пример registry: `config/products.example.json`; пример runtime-настроек: `deploy/internal.env.example`.

```sh
.venv/bin/support-agent provision --config config/products.example.json --dry-run
```

Пример содержит условные IDs; реальные `sub` и права назначает администратор. Рабочие `.env`, токены, сертификаты и ключи остаются вне Git и Docker build context. [Полный контракт и порядок подключения](docs/integrations.md).

Внутренний путь дополнительно проверяется через локальный HTTPS-шлюз с собственным CA и подписанными JWT. Это проверка протокола и workflow на синтетических ответах, не измерение Qwen и не подтверждение API компании.
