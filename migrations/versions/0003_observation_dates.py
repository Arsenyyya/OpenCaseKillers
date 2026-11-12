"""Stable observation times across source versions, without retaining removed text."""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"


def upgrade():
    op.create_table(
        "observations",
        sa.Column("product_id", sa.String(60), primary_key=True),
        sa.Column("id", sa.String(180), primary_key=True),
        sa.Column("ticket_id", sa.String(180), nullable=False),
        sa.Column("need_hash", sa.String(64), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["product_id", "ticket_id"], ["tickets.product_id", "tickets.id"]),
        sa.UniqueConstraint("product_id", "ticket_id", "need_hash"),
    )
    op.execute("ALTER TABLE observations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE observations FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY product_scope ON observations USING (product_id = ANY(string_to_array(current_setting('app.products',true),','))) WITH CHECK (product_id = ANY(string_to_array(current_setting('app.products',true),',')))"
    )
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON observations TO agent_worker")
    op.execute("GRANT SELECT ON observations TO agent_api")


def downgrade():
    op.drop_table("observations")
