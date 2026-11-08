"""Transactional domain changes shared by ingest, workers, API and CLI."""

from collections import defaultdict
from datetime import timedelta
from sqlalchemy import select, delete
from support_agent.db.models import (
    Ticket,
    Revision,
    Need,
    Membership,
    Group,
    Card,
    Outbox,
    Digest,
    MediaCache,
    Job,
    Receipt,
    Audit,
    Observation,
)
from support_agent.policy import digest


def audit(s, p, event, ref, now, actor="service"):
    s.add(Audit(product_id=p, event=event, actor=actor, ref=ref, created_at=now))


def invalidate(s, p, now, group_ids=None):
    if group_ids is not None and not group_ids:
        return
    # Invalidate only groups affected by a material source change.
    for c in s.scalars(select(Card).where(Card.product_id == p).with_for_update()):
        if group_ids is not None and c.group_id not in group_ids:
            continue
        if c.status not in ("stale", "correction_required"):
            c.status = "correction_required" if c.status == "delivered" else "stale"
    for o in s.scalars(select(Outbox).where(Outbox.product_id == p).with_for_update()):
        card = s.get(Card, (p, o.card_id))
        if group_ids is not None and card.group_id not in group_ids:
            continue
        if o.status in ("pending", "sending"):
            o.status = "cancelled"
        elif o.status == "delivered":
            o.status = "correction_required"
    s.execute(delete(Digest).where(Digest.product_id == p))


def rebuild_memberships(s, p):
    s.flush()
    s.execute(delete(Membership).where(Membership.product_id == p))
    rows = s.scalars(
        select(Need)
        .join(Revision, (Revision.product_id == Need.product_id) & (Revision.id == Need.revision_id))
        .join(Ticket, (Ticket.product_id == Need.product_id) & (Ticket.id == Need.ticket_id))
        .where(
            Need.product_id == p,
            Need.eligible.is_(True),
            Ticket.deleted.is_(False),
            Revision.number == Ticket.current_revision,
        )
    ).all()
    buckets = defaultdict(list)
    for n in rows:
        buckets[(n.group_id, n.origin_id)].append(n)
    for (gid, oid), ns in buckets.items():
        s.add(Membership(product_id=p, group_id=gid, origin_id=oid, first_seen=min(n.first_seen for n in ns)))
    for g in s.scalars(select(Group).where(Group.product_id == p)):
        ns = [n for n in rows if n.group_id == g.id]
        fp = digest(sorted((n.ticket_id, n.revision_id, n.origin_id, digest(n.evidence)) for n in ns))
        if fp != g.fingerprint:
            g.revision += 1
            g.fingerprint = fp
        g.active = bool(ns)
        if ns:
            # Canonical labels must always derive from a remaining allowed source.
            n = min(ns, key=lambda n: (n.first_seen, n.id))
            g.need, g.context = n.need, n.context
            g.first_seen = min(n.first_seen for n in ns)
        elif not s.scalar(select(Need.id).where(Need.product_id == p, Need.group_id == g.id).limit(1)):
            g.need, g.context = "[removed]", ""


def remove_ticket_content(s, ticket, now, deleted=True):
    p = ticket.product_id
    revisions = list(
        s.scalars(select(Revision).where(Revision.product_id == p, Revision.ticket_id == ticket.id))
    )
    ids = [r.id for r in revisions]
    affected = set(s.scalars(select(Need.group_id).where(Need.product_id == p, Need.ticket_id == ticket.id)))
    s.execute(delete(Need).where(Need.product_id == p, Need.ticket_id == ticket.id))
    if ids:
        s.execute(delete(MediaCache).where(MediaCache.product_id == p, MediaCache.revision_id.in_(ids)))
        for j in s.scalars(
            select(Job).where(
                Job.product_id == p,
                Job.revision_id.in_(ids),
                Job.status.in_(["queued", "running", "retry_wait"]),
            )
        ):
            j.status, j.claim_token, j.lease_until = "cancelled", None, None
    if deleted:
        s.execute(delete(Observation).where(Observation.product_id == p, Observation.ticket_id == ticket.id))
        ticket.deleted = True
        for r in revisions:
            r.raw, r.segments, r.coverage, r.status = None, None, {}, "deleted"
        # Clear copies and all evidence in local snapshots, receipts and pending work.
        for c in s.scalars(select(Card).where(Card.product_id == p)):
            if c.group_id in affected:
                c.snapshot = {}
        for r in s.scalars(select(Receipt).where(Receipt.product_id == p)):
            if r.payload.get("group_id") in affected:
                r.payload = {"withdrawal_required": True}
    invalidate(s, p, now, affected)
    s.flush()
    rebuild_memberships(s, p)
    audit(s, p, "ticket_deleted" if deleted else "revision_replaced", ticket.id, now)


def retention_preview(s, p, now, days):
    cutoff = now - timedelta(days=days)
    return list(
        s.scalars(
            select(Ticket.id).where(
                Ticket.product_id == p, Ticket.deleted.is_(False), Ticket.updated_at < cutoff
            )
        )
    )


def retention_run(s, p, now, days):
    ids = retention_preview(s, p, now, days)
    for tid in ids:
        ticket = s.scalar(select(Ticket).where(Ticket.product_id == p, Ticket.id == tid).with_for_update())
        remove_ticket_content(s, ticket, now)
    return ids
