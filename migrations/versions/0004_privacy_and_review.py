"""Bounded retention support, stable group observation, reviews and service privileges."""

from uuid import uuid4
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"


def upgrade():
    op.execute("GRANT SELECT ON alembic_version TO agent_worker, agent_api")
    op.add_column("revisions", sa.Column("extracted_items", postgresql.JSONB(), nullable=True))
    op.add_column("needs", sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("needs", sa.Column("review_reason", sa.String(300), nullable=True))
    op.add_column("outbox", sa.Column("resolution_reason", sa.String(500), nullable=True))
    op.add_column("outbox", sa.Column("resolved_by", sa.String(100), nullable=True))
    op.add_column("outbox", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("ALTER TABLE needs DISABLE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE needs SET needs_review=true, review_reason='Prior analysis requires review' WHERE NOT eligible"
    )
    op.create_table(
        "group_observations",
        sa.Column("product_id", sa.String(60), primary_key=True),
        sa.Column("id", sa.String(180), primary_key=True),
        sa.Column("ticket_id", sa.String(180), nullable=False),
        sa.Column("group_id", sa.String(180), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["product_id", "ticket_id"], ["tickets.product_id", "tickets.id"]),
        sa.ForeignKeyConstraint(["product_id", "group_id"], ["groups.product_id", "groups.id"]),
        sa.UniqueConstraint("product_id", "ticket_id", "group_id"),
    )
    # Preserve existing observations on upgrade, including earliest occurrence per group.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("""SELECT product_id,ticket_id,group_id,min(first_seen) AS first_seen
                                  FROM needs GROUP BY product_id,ticket_id,group_id""")
    ).mappings()
    for row in rows:
        bind.execute(
            sa.text("""INSERT INTO group_observations(product_id,id,ticket_id,group_id,first_seen)
            VALUES (:product_id,:id,:ticket_id,:group_id,:first_seen)"""),
            dict(row) | {"id": str(uuid4())},
        )
    op.execute("ALTER TABLE needs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE group_observations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE group_observations FORCE ROW LEVEL SECURITY")
    scope = "product_id = ANY(string_to_array(current_setting('app.products',true),','))"
    op.execute(f"CREATE POLICY product_scope ON group_observations USING ({scope}) WITH CHECK ({scope})")
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON group_observations TO agent_worker")
    op.execute("GRANT SELECT ON group_observations TO agent_api")
    op.create_index("revision_retention", "revisions", ["product_id", "created_at"])
    op.create_index("need_ticket_groups", "needs", ["product_id", "ticket_id", "group_id"])
    op.create_unique_constraint("revision_ticket_identity", "revisions", ["product_id", "id", "ticket_id"])
    op.create_foreign_key(
        "need_revision_ticket",
        "needs",
        "revisions",
        ["product_id", "revision_id", "ticket_id"],
        ["product_id", "id", "ticket_id"],
    )
    op.create_check_constraint("product_safe_id", "products", "id ~ '^[A-Za-z0-9_-]{1,60}$'")
    op.create_check_constraint(
        "grant_known_role", "grants", "role IN ('viewer','product_reviewer','operator')"
    )
    op.execute("REVOKE INSERT,UPDATE,DELETE ON products,grants FROM agent_worker")
    op.execute("REVOKE INSERT,DELETE ON provider_slots,product_leases FROM agent_worker")
    # The API mutates audited outbox reconciliation state, never source/analysis facts.
    op.execute("GRANT UPDATE ON outbox,decisions,product_leases TO agent_api")
    op.execute("GRANT INSERT ON metric_events TO agent_api")


def downgrade():
    op.execute("REVOKE INSERT ON metric_events FROM agent_api")
    op.execute("REVOKE UPDATE ON outbox,decisions,product_leases FROM agent_api")
    op.execute("GRANT INSERT,UPDATE,DELETE ON products,grants TO agent_worker")
    op.execute("GRANT INSERT,DELETE ON provider_slots,product_leases TO agent_worker")
    op.drop_constraint("grant_known_role", "grants", type_="check")
    op.drop_constraint("product_safe_id", "products", type_="check")
    op.drop_constraint("need_revision_ticket", "needs", type_="foreignkey")
    op.drop_constraint("revision_ticket_identity", "revisions", type_="unique")
    op.drop_index("need_ticket_groups", table_name="needs")
    op.drop_index("revision_retention", table_name="revisions")
    op.drop_table("group_observations")
    for column in ["resolved_at", "resolved_by", "resolution_reason"]:
        op.drop_column("outbox", column)
    op.drop_column("needs", "review_reason")
    op.drop_column("needs", "needs_review")
    op.drop_column("revisions", "extracted_items")
