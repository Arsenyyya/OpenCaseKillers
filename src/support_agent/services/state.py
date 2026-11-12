"""Transactional domain changes shared by ingest, workers, API and CLI."""

from collections import defaultdict
from datetime import timedelta
from sqlalchemy import select, delete, func, or_, and_, JSON
from sqlalchemy.orm import load_only
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
    GroupObservation,
    Decision,
)
from support_agent.policy import digest


MAX_RETENTION_BATCH = 500


def audit(s, p, event, ref, now, actor="service"):
    s.add(Audit(product_id=p, event=event, actor=actor, ref=ref, created_at=now))


def invalidate(s, p, now, group_ids=None):
    if group_ids is not None and not group_ids:
        return
    # Product lease serializes this short update with source changes and delivery snapshots.
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
    current = list(
        s.scalars(
            select(Need)
            .join(Revision, (Revision.product_id == Need.product_id) & (Revision.id == Need.revision_id))
            .join(Ticket, (Ticket.product_id == Need.product_id) & (Ticket.id == Need.ticket_id))
            .where(
                Need.product_id == p,
                Ticket.deleted.is_(False),
                Revision.number == Ticket.current_revision,
                Revision.status != "deleted",
            )
        )
    )
    buckets = defaultdict(list)
    by_group = defaultdict(list)
    for n in current:
        by_group[n.group_id].append(n)
        if n.eligible:
            buckets[(n.group_id, n.origin_id)].append(n)
    for (gid, oid), ns in buckets.items():
        s.add(Membership(product_id=p, group_id=gid, origin_id=oid, first_seen=min(n.first_seen for n in ns)))
    for g in s.scalars(select(Group).where(Group.product_id == p)):
        retained = by_group[g.id]
        ns = [n for n in retained if n.eligible]
        fp = digest(sorted((n.ticket_id, n.revision_id, n.origin_id, digest(n.evidence)) for n in ns))
        if fp != g.fingerprint:
            g.revision += 1
            g.fingerprint = fp
        g.active = bool(ns)
        if retained:
            # Provisional/partial needs remain visible without contributing to frequency.
            n = min(ns or retained, key=lambda n: (n.first_seen, n.id))
            g.need, g.context = n.need, n.context
            g.first_seen = min(n.first_seen for n in ns or retained)
            g.provisional = not bool(ns)
        else:
            g.need, g.context = "[removed]", ""


def _references(value, ticket_ids, revision_ids):
    """Inspect structured provenance, never guesses based on coincidental quote text."""
    if isinstance(value, dict):
        if value.get("ticket_id") in ticket_ids or value.get("revision_id") in revision_ids:
            return True
        return any(_references(v, ticket_ids, revision_ids) for v in value.values())
    if isinstance(value, list):
        return any(_references(v, ticket_ids, revision_ids) for v in value)
    return False


def _purge_copies(s, p, now, *, ticket_ids=(), revision_ids=(), group_ids=()):
    """Erase stale historical copies even when the ticket left its previous group."""
    tids, rids, groups = set(ticket_ids), set(revision_ids), set(group_ids)
    card_ids = set()
    for c in s.scalars(select(Card).where(Card.product_id == p).with_for_update()):
        if c.group_id in groups or _references(c.snapshot, tids, rids):
            c.snapshot = {}
            card_ids.add(c.id)
            groups.add(c.group_id)
    for receipt in s.scalars(select(Receipt).where(Receipt.product_id == p).with_for_update()):
        if (isinstance(receipt.payload, dict) and receipt.payload.get("group_id") in groups) or _references(
            receipt.payload, tids, rids
        ):
            receipt.payload = {"withdrawal_required": True}
    for snapshot in s.scalars(select(Digest).where(Digest.product_id == p)):
        if _references(snapshot.snapshot, tids, rids):
            s.delete(snapshot)
    if card_ids:
        # Review comments may quote source content; actor/time/outcome remain as minimal audit.
        for decision in s.scalars(
            select(Decision).where(Decision.product_id == p, Decision.card_id.in_(card_ids))
        ):
            decision.reason = "[content removed]"
        for out in s.scalars(select(Outbox).where(Outbox.product_id == p, Outbox.card_id.in_(card_ids))):
            out.resolution_reason = None
    invalidate(s, p, now, groups)


def _cancel_jobs(s, p, revision_ids):
    if not revision_ids:
        return
    for j in s.scalars(
        select(Job).where(
            Job.product_id == p,
            Job.revision_id.in_(revision_ids),
            Job.status.in_(["queued", "running", "retry_wait"]),
        )
    ):
        j.status, j.claim_token, j.lease_until = "cancelled", None, None


def _clear_revisions(s, p, revisions, now, *, ticket_ids=()):
    ids = [r.id for r in revisions]
    if not ids:
        return
    groups = set(s.scalars(select(Need.group_id).where(Need.product_id == p, Need.revision_id.in_(ids))))
    s.execute(delete(Need).where(Need.product_id == p, Need.revision_id.in_(ids)))
    s.execute(delete(MediaCache).where(MediaCache.product_id == p, MediaCache.revision_id.in_(ids)))
    _cancel_jobs(s, p, ids)
    for r in revisions:
        r.raw, r.segments, r.extracted_items, r.coverage, r.status = None, None, None, {}, "deleted"
    _purge_copies(s, p, now, ticket_ids=ticket_ids, revision_ids=ids, group_ids=groups)


def remove_ticket_content(s, ticket, now, deleted=True):
    p = ticket.product_id
    revisions = list(
        s.scalars(
            select(Revision)
            .where(Revision.product_id == p, Revision.ticket_id == ticket.id)
            .options(load_only(Revision.product_id, Revision.id, Revision.ticket_id, Revision.number))
        )
    )
    ids = [r.id for r in revisions]
    if deleted:
        ticket.deleted = True
        _clear_revisions(s, p, revisions, now, ticket_ids=[ticket.id])
        # A tombstone with no accepted content can still have copied evidence after an earlier purge.
        _purge_copies(s, p, now, ticket_ids=[ticket.id])
        s.execute(delete(Observation).where(Observation.product_id == p, Observation.ticket_id == ticket.id))
        s.execute(
            delete(GroupObservation).where(
                GroupObservation.product_id == p, GroupObservation.ticket_id == ticket.id
            )
        )
    else:
        affected = set(
            s.scalars(select(Need.group_id).where(Need.product_id == p, Need.ticket_id == ticket.id))
        )
        s.execute(delete(Need).where(Need.product_id == p, Need.ticket_id == ticket.id))
        # Old raw/segments are retained only until their own hard revision TTL.
        s.execute(delete(MediaCache).where(MediaCache.product_id == p, MediaCache.revision_id.in_(ids)))
        _cancel_jobs(s, p, ids)
        invalidate(s, p, now, affected)
    s.flush()
    rebuild_memberships(s, p)
    audit(s, p, "ticket_deleted" if deleted else "revision_replaced", ticket.id, now)


def _retention_query(p, now, days):
    if days < 1:
        raise ValueError("retention days must be positive")
    # JSON null and SQL NULL both mean no remaining content. Old releases stored JSON null.
    content = or_(
        and_(Revision.raw.is_not(None), Revision.raw != JSON.NULL),
        and_(Revision.segments.is_not(None), Revision.segments != JSON.NULL),
        and_(Revision.extracted_items.is_not(None), Revision.extracted_items != JSON.NULL),
    )
    return select(Revision).where(
        Revision.product_id == p, Revision.created_at < now - timedelta(days=days), content
    )


def retention_plan(s, p, now, days, limit=100, offset=0):
    if not 1 <= limit <= MAX_RETENTION_BATCH or not 0 <= offset <= 100000:
        raise ValueError("retention pagination is out of bounds")
    query = _retention_query(p, now, days)
    total = s.scalar(select(func.count()).select_from(query.subquery()))
    rows = list(
        s.execute(
            query.with_only_columns(Revision.id, Revision.ticket_id)
            .order_by(Revision.created_at, Revision.id)
            .limit(limit)
            .offset(offset)
        )
    )
    return {
        "ticket_ids": sorted({r.ticket_id for r in rows}),
        "revision_ids": [r.id for r in rows],
        "total_revisions": total,
        "has_more": offset + len(rows) < total,
        "limit": limit,
        "offset": offset,
    }


def retention_preview(s, p, now, days, limit=100, offset=0):
    return retention_plan(s, p, now, days, limit, offset)["ticket_ids"]


def retention_run(s, p, now, days, limit=100):
    plan = retention_plan(s, p, now, days, limit=limit)
    ids = set(plan["revision_ids"])
    if not ids:
        return []
    touched = []
    for tid in plan["ticket_ids"]:
        ticket = s.scalar(select(Ticket).where(Ticket.product_id == p, Ticket.id == tid).with_for_update())
        revisions = list(
            s.scalars(
                select(Revision)
                .where(Revision.product_id == p, Revision.ticket_id == tid, Revision.id.in_(ids))
                .options(load_only(Revision.product_id, Revision.id, Revision.ticket_id, Revision.number))
            )
        )
        if any(r.number == ticket.current_revision for r in revisions):
            remove_ticket_content(s, ticket, now)
        else:
            _clear_revisions(s, p, revisions, now)
            s.flush()
            rebuild_memberships(s, p)
            audit(s, p, "historical_content_expired", ticket.id, now)
        touched.append(tid)
    s.flush()
    return sorted(touched)
