"""Stable observation times across source versions, without retaining removed text."""

from alembic import op
from support_agent.db.models import Observation

revision = "0003"
down_revision = "0002"


def upgrade():
    Observation.__table__.create(op.get_bind(), checkfirst=True)
    op.execute("ALTER TABLE observations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE observations FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS product_scope ON observations")
    op.execute(
        "CREATE POLICY product_scope ON observations USING (product_id = ANY(string_to_array(current_setting('app.products',true),','))) WITH CHECK (product_id = ANY(string_to_array(current_setting('app.products',true),',')))"
    )
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON observations TO agent_worker")
    op.execute("GRANT SELECT ON observations TO agent_api")


def downgrade():
    op.execute("DROP TABLE observations")
