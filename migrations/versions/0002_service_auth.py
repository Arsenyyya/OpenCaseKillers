"""Allow the demo orchestrator/service to verify server-side principals."""

from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade():
    op.execute("GRANT SELECT ON principals TO agent_worker")


def downgrade():
    op.execute("REVOKE SELECT ON principals FROM agent_worker")
