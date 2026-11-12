PYTHON ?= python3
PY := .venv/bin/python
UV := .venv/bin/uv
AGENT := .venv/bin/support-agent
CACHE := .cache/uv

.PHONY: help install install-docs bootstrap db migrate provision doctor demo verify api \
	test lint format schemas schemas-check check wheel check-wheel report image docker-demo up stop logs

help:
	@echo "OpenCaseKillers — команды из корня проекта"
	@echo "  make demo         установить зависимости, поднять БД, пройти reviewed demo"
	@echo "  make verify       чистая временная БД: restart, replay, update/delete"
	@echo "  make api          локальный API под ограниченной DB-ролью"
	@echo "  make check        lint, формат, schema drift и полный pytest с PostgreSQL"
	@echo "  make check-wheel  собрать wheel и проверить установку вне checkout"
	@echo "  make doctor       проверить конфигурацию и обязательные зависимости"
	@echo "  make provision CONFIG=...  загрузить явную серверную конфигурацию"
	@echo "  make docker-demo  собрать образ и пройти demo внутри Docker"
	@echo "  make up / stop    запустить / остановить сервисы, сохранив данные"
	@echo "  make report       собрать PDF-отчёт с DejaVu Sans"

$(PY):
	$(PYTHON) -m venv .venv

$(UV): $(PY)
	$(PY) -m pip install uv==0.13.7

install: $(UV)
	$(UV) sync --frozen --extra dev --cache-dir $(CACHE)

install-docs: $(UV)
	$(UV) sync --frozen --extra dev --extra docs --cache-dir $(CACHE)

bootstrap:
	$(PYTHON) scripts/bootstrap.py

db: bootstrap
	docker compose up -d --wait db

migrate: install db
	$(AGENT) migrate

doctor: install
	$(AGENT) doctor

provision: install
	@test -n "$(CONFIG)" || { echo "Use make provision CONFIG=.local/provision.json"; exit 2; }
	$(AGENT) provision --config "$(CONFIG)"

demo: migrate
	DELIVERY_MODE=reviewed $(AGENT) run-demo

verify: migrate
	$(PY) scripts/verify_demo.py

api: migrate
	$(PY) scripts/start_api.py

test: migrate
	$(PY) -m pytest -q --junitxml=.local/test-results.xml

lint: install
	.venv/bin/ruff check src tests scripts migrations
	.venv/bin/ruff format --check src tests scripts migrations

format: install
	.venv/bin/ruff format src tests scripts migrations

schemas: install
	$(PY) scripts/export_schemas.py

schemas-check: install
	$(PY) scripts/export_schemas.py --check

check: lint schemas-check test

wheel: install
	$(UV) build --wheel --out-dir .local/dist --cache-dir $(CACHE)

check-wheel: wheel
	$(PY) scripts/check_wheel.py

report: install-docs
	$(PY) scripts/build_report.py

image: bootstrap
	sh scripts/build-image.sh

docker-demo: image db
	docker compose run --rm --no-deps -T migrate support-agent migrate
	docker compose run --rm --no-deps -T -e DELIVERY_MODE=reviewed bootstrap support-agent run-demo

up: image db
	docker compose up -d --no-build --wait --wait-timeout 60

stop:
	docker compose stop

logs:
	docker compose logs --tail 100
