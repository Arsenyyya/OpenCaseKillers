from alembic import context
from sqlalchemy import create_engine
from support_agent.settings import AdminSettings
from support_agent.db.models import Base


def run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


provided = context.config.attributes.get("connection")
if provided is not None:
    run(provided)
else:
    engine = create_engine(AdminSettings().database_admin_url)
    with engine.connect() as connection:
        run(connection)
    engine.dispose()
