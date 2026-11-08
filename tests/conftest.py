import pytest
from datetime import datetime
from sqlalchemy import text
from support_agent.application import Application
from support_agent.settings import Settings
from support_agent.clock import FrozenClock
from support_agent.db.testing import isolated_database
from support_agent.db.session import Database
from support_agent.db.models import Base
from support_agent.demo import seed_demo


@pytest.fixture(scope="session")
def database_settings():
    settings = Settings()
    with isolated_database(settings) as test_settings:
        yield test_settings


@pytest.fixture
def app(database_settings, tmp_path):
    admin = Database(database_settings.database_admin_url)
    with admin.tx() as s:
        names = ",".join(t.name for t in Base.metadata.sorted_tables)
        s.execute(text(f"TRUNCATE {names} CASCADE"))
    admin.close()
    cfg = database_settings.model_copy(
        update={"token_file": tmp_path / "tokens.json", "delivery_mode": "reviewed"}
    )
    app = Application(cfg, clock=FrozenClock(datetime.fromisoformat("2026-11-06T12:00:00+00:00")))
    app.tokens = seed_demo(app)
    yield app
    app.db.close()
