import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, Table, Column, Integer
from support_agent.settings import ROOT
from support_agent.db.models import Base
from support_agent.db.testing import isolated_database
from support_agent.db.session import Database


def migrate(connection, target, *, down=False):
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["connection"] = connection
    (command.downgrade if down else command.upgrade)(cfg, target)
    connection.commit()


def test_migrations_are_frozen_and_roundtrip_without_live_metadata(database_settings):
    # Simulate a future application model: historical migrations must never create it.
    phantom = Table("future_model_not_in_migrations", Base.metadata, Column("id", Integer, primary_key=True))
    try:
        with isolated_database(database_settings) as cfg:
            engine = create_engine(cfg.database_admin_url)
            try:
                with engine.connect() as c:
                    assert "future_model_not_in_migrations" not in inspect(c).get_table_names()
                    migrate(c, "0001", down=True)
                    tables = inspect(c).get_table_names()
                    assert "observations" not in tables and "group_observations" not in tables
                    assert "extracted_items" not in {x["name"] for x in inspect(c).get_columns("revisions")}
                    migrate(c, "0003")
                    assert "observations" in inspect(c).get_table_names()
                    assert "group_observations" not in inspect(c).get_table_names()
                    runtime = Database(cfg.database_url)
                    try:
                        with pytest.raises(RuntimeError, match="migration head"):
                            runtime.runtime_ready()
                    finally:
                        runtime.close()
                    migrate(c, "head")
                    assert "group_observations" in inspect(c).get_table_names()
                    assert "extracted_items" in {x["name"] for x in inspect(c).get_columns("revisions")}
                    assert "future_model_not_in_migrations" not in inspect(c).get_table_names()
                    migrate(c, "base", down=True)
                    assert set(inspect(c).get_table_names()) == {"alembic_version"}
                    migrate(c, "head")
            finally:
                engine.dispose()
    finally:
        Base.metadata.remove(phantom)
