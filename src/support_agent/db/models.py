from datetime import datetime
from uuid import uuid4
from sqlalchemy import (
    String,
    Integer,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    UniqueConstraint,
    Index,
    Float,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB


def uid():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Scoped:
    product_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    id: Mapped[str] = mapped_column(String(180), primary_key=True, default=uid)


def fk(table, col, target="id", **kw):
    return ForeignKeyConstraint(["product_id", col], [f"{table}.product_id", f"{table}.{target}"], **kw)


def product_fk():
    return ForeignKeyConstraint(["product_id"], ["products.id"])


class Product(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    owner: Mapped[str] = mapped_column(String(100))
    terms: Mapped[dict] = mapped_column(JSONB, default=dict)


class Principal(Base):
    __tablename__ = "principals"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Grant(Scoped, Base):
    __tablename__ = "grants"
    actor: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(30))
    __table_args__ = (
        product_fk(),
        ForeignKeyConstraint(["actor"], ["principals.id"]),
        UniqueConstraint("product_id", "actor", "role"),
    )


class Cursor(Scoped, Base):
    __tablename__ = "cursors"
    position: Mapped[str] = mapped_column(String(200), default="0")
    initialized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (product_fk(),)


class Ticket(Scoped, Base):
    __tablename__ = "tickets"
    source_id: Mapped[str] = mapped_column(String(60))
    external_id: Mapped[str] = mapped_column(String(100))
    source_seq: Mapped[int] = mapped_column(BigInteger)
    source_version: Mapped[str] = mapped_column(String(100))
    current_revision: Mapped[int] = mapped_column(Integer)
    origin_id: Mapped[str] = mapped_column(String(180))
    duplicate_of: Mapped[str | None] = mapped_column(String(180))
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    channel: Mapped[str] = mapped_column(String(30))
    __table_args__ = (
        product_fk(),
        fk("tickets", "duplicate_of"),
        UniqueConstraint("product_id", "source_id", "external_id"),
        Index("ticket_changes", "product_id", "updated_at"),
    )


class Revision(Scoped, Base):
    __tablename__ = "revisions"
    ticket_id: Mapped[str] = mapped_column(String(180))
    number: Mapped[int] = mapped_column(Integer)
    source_version: Mapped[str] = mapped_column(String(100))
    content_hash: Mapped[str] = mapped_column(String(64))
    raw: Mapped[dict | None] = mapped_column(JSONB)
    segments: Mapped[list | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(30), default="accepted")
    coverage: Mapped[dict] = mapped_column(JSONB, default=dict)
    normalization_version: Mapped[str] = mapped_column(String(40), default="norm-v1")
    masking_version: Mapped[str] = mapped_column(String(40), default="mask-v1")
    model_id: Mapped[str | None] = mapped_column(String(60))
    prompt_version: Mapped[str | None] = mapped_column(String(60))
    schema_version: Mapped[str] = mapped_column(String(40), default="extraction-v1")
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    usage: Mapped[dict] = mapped_column(JSONB, default=dict)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (fk("tickets", "ticket_id"), UniqueConstraint("product_id", "ticket_id", "number"))


class MediaCache(Scoped, Base):
    __tablename__ = "media_cache"
    revision_id: Mapped[str] = mapped_column(String(180))
    content_hash: Mapped[str] = mapped_column(String(64))
    processor_version: Mapped[str] = mapped_column(String(100))
    settings_hash: Mapped[str] = mapped_column(String(64))
    segments: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        fk("revisions", "revision_id"),
        UniqueConstraint("product_id", "revision_id", "content_hash", "processor_version", "settings_hash"),
    )


class Job(Scoped, Base):
    __tablename__ = "jobs"
    revision_id: Mapped[str | None] = mapped_column(String(180))
    kind: Mapped[str] = mapped_column(String(30))
    dedupe_key: Mapped[str] = mapped_column(String(220))
    priority: Mapped[int] = mapped_column(Integer, default=10)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(40))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        product_fk(),
        fk("revisions", "revision_id"),
        UniqueConstraint("product_id", "dedupe_key"),
        Index("jobs_claim", "product_id", "kind", "status", "priority", "available_at"),
    )


class ProductLease(Scoped, Base):
    __tablename__ = "product_leases"
    token: Mapped[str | None] = mapped_column(String(40))
    until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (product_fk(),)


class ProviderSlot(Base):
    __tablename__ = "provider_slots"
    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    provider: Mapped[str] = mapped_column(String(30))
    token: Mapped[str | None] = mapped_column(String(40))
    until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("provider_capacity", "provider", "until"),)


class Group(Scoped, Base):
    __tablename__ = "groups"
    kind: Mapped[str] = mapped_column(String(30))
    need: Mapped[str] = mapped_column(String(500))
    context: Mapped[str] = mapped_column(String(500))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    provisional: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (product_fk(), Index("groups_active", "product_id", "active", "kind"))


class Need(Scoped, Base):
    __tablename__ = "needs"
    revision_id: Mapped[str] = mapped_column(String(180))
    group_id: Mapped[str] = mapped_column(String(180))
    ticket_id: Mapped[str] = mapped_column(String(180))
    origin_id: Mapped[str] = mapped_column(String(180))
    kind: Mapped[str] = mapped_column(String(30))
    need: Mapped[str] = mapped_column(String(500))
    context: Mapped[str] = mapped_column(String(500))
    urgency: Mapped[str] = mapped_column(String(30))
    evidence: Mapped[list] = mapped_column(JSONB)
    eligible: Mapped[bool] = mapped_column(Boolean)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        fk("revisions", "revision_id"),
        fk("groups", "group_id"),
        fk("tickets", "ticket_id"),
        UniqueConstraint("product_id", "revision_id", "group_id"),
    )


class Membership(Scoped, Base):
    __tablename__ = "memberships"
    group_id: Mapped[str] = mapped_column(String(180))
    origin_id: Mapped[str] = mapped_column(String(180))
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        fk("groups", "group_id"),
        UniqueConstraint("product_id", "group_id", "origin_id"),
        Index("memberships_window", "product_id", "group_id", "first_seen"),
    )


class Card(Scoped, Base):
    __tablename__ = "cards"
    group_id: Mapped[str] = mapped_column(String(180))
    revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(40), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (fk("groups", "group_id"), UniqueConstraint("product_id", "group_id"))


class Digest(Scoped, Base):
    __tablename__ = "digests"
    period: Mapped[str] = mapped_column(String(50))
    snapshot: Mapped[list] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (product_fk(), UniqueConstraint("product_id", "period", "content_hash"))


class Decision(Scoped, Base):
    __tablename__ = "decisions"
    card_id: Mapped[str] = mapped_column(String(180))
    revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(100))
    decision: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (fk("cards", "card_id"), UniqueConstraint("product_id", "card_id", "revision"))


class Outbox(Scoped, Base):
    __tablename__ = "outbox"
    card_id: Mapped[str] = mapped_column(String(180))
    decision_id: Mapped[str] = mapped_column(String(180))
    revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    correlation_key: Mapped[str] = mapped_column(String(250), unique=True)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    external_id: Mapped[str | None] = mapped_column(String(100))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (fk("cards", "card_id"), fk("decisions", "decision_id"))


class Receipt(Scoped, Base):
    __tablename__ = "receipts"
    correlation_key: Mapped[str] = mapped_column(String(250), unique=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (product_fk(),)


class Audit(Scoped, Base):
    __tablename__ = "audit"
    event: Mapped[str] = mapped_column(String(60))
    actor: Mapped[str] = mapped_column(String(100))
    ref: Mapped[str] = mapped_column(String(180))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (product_fk(),)


class Metric(Scoped, Base):
    __tablename__ = "metric_events"
    name: Mapped[str] = mapped_column(String(60))
    value: Mapped[float] = mapped_column(Float)
    channel: Mapped[str] = mapped_column(String(30), default="all")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (product_fk(),)


class Observation(Scoped, Base):
    __tablename__ = "observations"
    ticket_id: Mapped[str] = mapped_column(String(180))
    need_hash: Mapped[str] = mapped_column(String(64))
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (fk("tickets", "ticket_id"), UniqueConstraint("product_id", "ticket_id", "need_hash"))
