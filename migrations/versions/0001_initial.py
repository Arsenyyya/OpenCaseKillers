"""Product-scoped pipeline, queue, audit and forced row security."""

from alembic import op
from support_agent.db.models import Base

revision = "0001"
down_revision = None


def upgrade():
    bind = op.get_bind()
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    Base.metadata.create_all(bind)
    op.execute("CREATE INDEX groups_fts ON groups USING gin(to_tsvector('russian', need || ' ' || context))")
    op.execute("CREATE INDEX groups_trgm ON groups USING gin(need gin_trgm_ops)")
    for table in Base.metadata.sorted_tables:
        name = table.name
        if name in ("principals", "provider_slots"):
            continue
        col = "id" if name == "products" else "product_id"
        predicate = f"{col} = ANY(string_to_array(current_setting('app.products', true), ','))"
        if name == "grants":
            predicate = f"({predicate} OR actor = current_setting('app.actor', true))"
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY product_scope ON {name} USING ({predicate}) WITH CHECK ({predicate})")
    op.execute("GRANT USAGE ON SCHEMA public TO agent_worker, agent_api")
    scoped = ",".join(t.name for t in Base.metadata.sorted_tables if t.name != "principals")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {scoped} TO agent_worker")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_api")
    op.execute("GRANT INSERT ON decisions,outbox,audit,jobs TO agent_api")
    op.execute("GRANT UPDATE ON cards,jobs TO agent_api")


def downgrade():
    Base.metadata.drop_all(op.get_bind())
