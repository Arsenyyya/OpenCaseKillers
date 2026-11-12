# Быстрый старт

## Предварительные условия

Python 3.12+, работающий Docker с Compose. На Windows команды выполняются в WSL; на macOS/Linux — в обычном терминале. Команды ниже запускаются из корня checkout. Порты 55432 и 8000 должны быть свободны либо уже обслуживать этот проект.

## Рекомендуемый путь: Python + PostgreSQL

```sh
make demo
make doctor
make verify
make api
```

`make demo` последовательно выполняет install, local bootstrap, запуск БД, миграции и reviewed demo. `make verify` создаёт отдельную временную БД, проверяет полный путь, рестарт, повторы, изменение и удаление и удаляет только эту временную БД. Рабочая demo-БД не очищается.

Те же шаги без make:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install uv==0.13.7
.venv/bin/uv sync --frozen --extra dev --cache-dir .cache/uv
python3 scripts/bootstrap.py
docker compose up -d --wait db
.venv/bin/support-agent migrate
DELIVERY_MODE=reviewed .venv/bin/support-agent run-demo
.venv/bin/support-agent doctor
.venv/bin/python scripts/verify_demo.py
.venv/bin/python scripts/start_api.py
```

Ожидаемый результат чистой демонстрации: 40 тикетов, 37 origin/group memberships, 6 карточек, 2 локальные доставки. Повтор `run-demo` не увеличивает частоту и число доставок. Дообучения или реального Qwen-вызова не происходит.

OpenAPI: http://127.0.0.1:8000/docs. `scripts/start_api.py` использует `API_DATABASE_URL`, workers — `DATABASE_URL`. Оба — runtime-роли без владения таблицами/BYPASSRLS. Admin URL используется отдельно для migrate/seed/provision.

Проверка доступа:

```sh
.venv/bin/python scripts/api_probe.py
```

Локальные токены находятся в `.local/tokens.json` с правами 0600. `viewer` читает Atlas, `reviewer` согласует Atlas, `boreal` — Boreal; `operator` управляет заданиями обоих продуктов, но не согласует карточки. Передавайте `Authorization: Bearer <token>`; роли из тела или иных заголовков не назначаются.

Если API уже работает в Docker, используйте его или укажите свободный порт:

```sh
.venv/bin/python scripts/start_api.py --port 8001
.venv/bin/python scripts/api_probe.py --url http://127.0.0.1:8001
```

## Docker

```sh
make docker-demo
make up
```

Эквивалентные шаги:

```sh
python3 scripts/bootstrap.py
sh scripts/build-image.sh
docker compose up -d --wait db
docker compose run --rm --no-deps -T migrate support-agent migrate
docker compose run --rm --no-deps -T -e DELIVERY_MODE=reviewed bootstrap support-agent run-demo
docker compose up -d --no-build --wait --wait-timeout 60
```

`migrate` получает только административную конфигурацию. `bootstrap` — явно вызываемый одноразовый tools-service для demo/provision. В обычные API/workers admin credentials не передаются. Для разбора production identity и CA используйте отдельную конфигурацию компании и read-only secret mounts; public example не содержит рабочих подключений.

После Docker seed credentials находятся в named volume. Проверяйте API из этого окружения, без копирования токенов:

```sh
docker compose exec -T ingest python scripts/api_probe.py --url http://delivery:8000 --tokens /app/.local/tokens.json
```

Host seed и container seed используют разные credential-файлы и обновляют серверный реестр по ним. Выберите один путь. Для обычного restart повторный seed не нужен.

## Проверки и пакет

```sh
make check
make check-wheel
make verify
```

Wheel проверяется после установки в отдельное окружение и запуске CLI из другой рабочей папки. Он содержит prompts, конфигурационные ресурсы, fixtures и миграции. Локальные credentials/storage по умолчанию создаются в `.local` текущей папки, а не в site-packages.

Для установленного wheel сначала создайте явные environment settings и credentials; затем доступны `support-agent migrate`, `support-agent provision`, `support-agent doctor` и обычные worker/API команды. Скрипты Makefile и `scripts/*` являются удобствами checkout и в wheel не требуются.

## Остановка

```sh
make stop
# Или отдельный процесс:
docker compose stop sender scheduler
```

`pgdata` и `agent-storage` сохраняются. Не используйте `down --volumes` для обычного restart: он удаляет локальные данные. В CI удаляются только созданные для проверки disposable volumes.
