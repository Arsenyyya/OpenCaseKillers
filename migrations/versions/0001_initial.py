"""Product-scoped pipeline, queue, audit and forced row security.

The schema is deliberately frozen: upgrading a fresh database must not depend on
application models from a later release.
"""

from pathlib import Path
from alembic import op

revision = "0001"
down_revision = None
TABLES = (
    "principals",
    "products",
    "provider_slots",
    "audit",
    "cursors",
    "digests",
    "grants",
    "groups",
    "metric_events",
    "product_leases",
    "receipts",
    "tickets",
    "cards",
    "memberships",
    "revisions",
    "decisions",
    "jobs",
    "media_cache",
    "needs",
    "outbox",
)


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    sql = Path(__file__).with_name("0001_schema.sql").read_text()
    for statement in sql.split(";"):
        if statement.strip():
            op.execute(statement)
    op.execute("CREATE INDEX groups_fts ON groups USING gin(to_tsvector('russian', need || ' ' || context))")
    op.execute("CREATE INDEX groups_trgm ON groups USING gin(need gin_trgm_ops)")
    for name in TABLES:
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
    op.execute("GRANT SELECT ON alembic_version TO agent_worker, agent_api")
    scoped = ",".join(name for name in TABLES if name != "principals")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {scoped} TO agent_worker")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_api")
    op.execute("GRANT INSERT ON decisions,outbox,audit,jobs TO agent_api")
    op.execute("GRANT UPDATE ON cards,jobs TO agent_api")


def downgrade():
    for name in reversed(TABLES):
        op.drop_table(name)
