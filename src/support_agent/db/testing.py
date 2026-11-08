"""Explicit disposable PostgreSQL databases for integration verification only."""

from contextlib import contextmanager
from uuid import uuid4
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from alembic import command
from alembic.config import Config
from support_agent.settings import ROOT


@contextmanager
def isolated_database(settings, prefix="agent_test_"):
    if settings.app_env != "local" or settings.mode != "demo" or prefix not in ("agent_test_", "agent_load_"):
        raise RuntimeError("Disposable databases are restricted to local demo verification")
    name = prefix + uuid4().hex[:12]
    admin_url = make_url(settings.database_admin_url)
    admin = create_engine(admin_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    test_admin = create_engine(admin_url.set(database=name))
    with admin.connect() as c:
        c.execute(text(f"CREATE DATABASE {name}"))  # generated identifier, never user input
    try:
        with test_admin.connect() as connection:
            cfg = Config(str(ROOT / "alembic.ini"))
            cfg.set_main_option("script_location", str(ROOT / "migrations"))
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
        yield settings.model_copy(
            update={
                "database_admin_url": admin_url.set(database=name).render_as_string(hide_password=False),
                "database_url": make_url(settings.database_url)
                .set(database=name)
                .render_as_string(hide_password=False),
            }
        )
    finally:
        test_admin.dispose()
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()
