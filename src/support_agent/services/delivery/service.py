from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from support_agent.db.models import (
    Group,
    Membership,
    Need,
    Revision,
    Card,
    Digest,
    Decision,
    Outbox,
    Product,
    ProductLease,
    Audit,
    Job,
    Grant,
    Principal,
    Ticket,
)
from support_agent.policy import digest, mask
from support_agent.queue import enqueue
from support_agent.errors import Conflict, NotFound, Permanent
from support_agent.services.state import audit


def period_window(app, period=None):
    tz = ZoneInfo(app.settings.business_timezone)
    day = date.fromisoformat(period) if period else app.clock.now().astimezone(tz).date()
    end = datetime.combine(day + timedelta(days=1), time(), tz)
    start = end - timedelta(days=app.settings.window_days)
    return day.isoformat(), start.astimezone(timezone.utc), min(end.astimezone(timezone.utc), app.clock.now())


def build_digest(app, p, period=None):
    app.service_scope(p)
    day, start, end = period_window(app, period)
    current_day = app.clock.now().astimezone(ZoneInfo(app.settings.business_timezone)).date().isoformat()
    if day > current_day:
        raise Conflict()
    if day < current_day:
        # Current active needs cannot reconstruct the historical as-of state after edits/deletions.
        with app.db.tx([p]) as s:
            saved = s.scalar(
                select(Digest)
                .where(Digest.product_id == p, Digest.period == day)
                .order_by(Digest.created_at.desc(), Digest.id.desc())
                .limit(1)
            )
            if not saved:
                raise NotFound()
            return {"period": day, "cards": saved.snapshot}
    with app.db.tx([p]) as s:
        # Serialize short snapshot writes with assignment/source commits, never model I/O.
        s.scalar(
            select(ProductLease)
            .where(ProductLease.product_id == p, ProductLease.id == "assignment")
            .with_for_update()
        )
        owner = s.get(Product, p).owner
        groups = s.scalars(
            select(Group).where(
                Group.product_id == p,
                Group.active.is_(True),
                Group.provisional.is_(False),
                Group.kind.in_(["feature_request", "defect"]),
            )
        ).all()
        ranked = []
        for g in groups:
            members = s.scalars(
                select(Membership).where(
                    Membership.product_id == p,
                    Membership.group_id == g.id,
                    Membership.first_seen >= start,
                    Membership.first_seen < end,
                )
            ).all()
            if len(members) < app.settings.threshold:
                continue
            growth = sum(m.first_seen >= end - timedelta(days=7) for m in members) - sum(
                end - timedelta(days=14) <= m.first_seen < end - timedelta(days=7) for m in members
            )
            ranked.append((g, members, growth))
        ranked.sort(
            key=lambda v: (
                0 if v[0].kind == "feature_request" else 1,
                -len(v[1]),
                -v[2],
                v[0].first_seen,
                v[0].id,
            )
        )
        tz = ZoneInfo(app.settings.business_timezone)
        day_start = datetime.combine(date.fromisoformat(day), time(), tz).astimezone(timezone.utc)
        prepared = set(
            s.scalars(
                select(Audit.ref).where(
                    Audit.product_id == p,
                    Audit.event == "card_prepared",
                    Audit.created_at >= day_start,
                    Audit.created_at < day_start + timedelta(days=1),
                )
            )
        )
        chosen = []
        # Only new or materially changed cards consume today's five-card budget.
        for g, members, growth in ranked:
            origins = {m.origin_id for m in members}
            ns = s.scalars(
                select(Need)
                .join(Revision, (Revision.product_id == Need.product_id) & (Revision.id == Need.revision_id))
                .join(Ticket, (Ticket.product_id == Need.product_id) & (Ticket.id == Need.ticket_id))
                .where(
                    Need.product_id == p,
                    Revision.number == Ticket.current_revision,
                    Ticket.deleted.is_(False),
                    Need.group_id == g.id,
                    Need.eligible.is_(True),
                    Need.origin_id.in_(origins),
                )
                .order_by(Need.first_seen, Need.id)
            ).all()
            sources, seen = [], set()
            for n in ns:
                if n.origin_id in seen:
                    continue
                seen.add(n.origin_id)
                r = s.get(Revision, (p, n.revision_id))
                sources.append(
                    {
                        "ticket_id": n.ticket_id,
                        "revision_id": r.id,
                        "source_version": r.source_version,
                        "content_hash": r.content_hash,
                        "origin_id": n.origin_id,
                        "evidence": n.evidence,
                        "coverage": r.coverage,
                        "model_id": r.model_id,
                    }
                )
            from support_agent.catalog import lookup_known_task

            payload = {
                "product_id": p,
                "group_id": g.id,
                "group_revision": g.revision,
                "kind": g.kind,
                "need": g.need,
                "context": g.context,
                "owner": owner,
                "independent_origins": len(members),
                "source_tickets": len(ns),
                "growth_7d": growth,
                "sources": sources[:3],
                "first_seen": g.first_seen.isoformat(),
                "known_backlog": lookup_known_task(s, p, g.kind, g.need),
                "limitations": "Synthetic demo; no measured Qwen quality"
                if app.settings.mode == "demo"
                else "ASR/OCR evidence confirms normalized text only",
            }
            h = digest(payload)
            card = s.scalar(select(Card).where(Card.product_id == p, Card.group_id == g.id).with_for_update())
            if (
                card
                and card.status != "stale"
                and card.snapshot
                and digest({k: v for k, v in card.snapshot.items() if k != "period"}) == h
                and card.content_hash == digest(card.snapshot)
            ):
                continue
            if len(prepared) >= 5 and (not card or card.id not in prepared):
                continue
            payload["period"] = {"start": start.isoformat(), "end": end.isoformat(), "business_date": day}
            h = digest(payload)  # Approval binds the entire displayed snapshot, including its period.
            if card:
                card.revision += 1
                card.content_hash, card.snapshot, card.status = h, payload, "draft"
            else:
                card = Card(
                    product_id=p,
                    group_id=g.id,
                    revision=1,
                    content_hash=h,
                    snapshot=payload,
                    status="draft",
                    created_at=app.clock.now(),
                )
                s.add(card)
                s.flush()
            prepared.add(card.id)
            chosen.append({"card_id": card.id, "revision": card.revision, "content_hash": card.content_hash})
            audit(s, p, "card_prepared", card.id, app.clock.now())
        # Use the same count/growth/wait tie-breaks for the final daily snapshot.
        daily_cards = list(
            s.scalars(
                select(Card).where(
                    Card.product_id == p,
                    Card.id.in_(prepared),
                    Card.status.not_in(["stale", "correction_required"]),
                )
            )
        )
        daily_cards = [card for card in daily_cards if card.snapshot]
        daily_cards.sort(
            key=lambda card: (
                0 if card.snapshot["kind"] == "feature_request" else 1,
                -card.snapshot["independent_origins"],
                -card.snapshot.get("growth_7d", 0),
                card.snapshot["first_seen"],
                card.group_id,
            )
        )
        chosen = [
            {
                "card_id": card.id,
                "revision": card.revision,
                "content_hash": card.content_hash,
                "kind": card.snapshot["kind"],
                "need": card.snapshot["need"],
                "independent_origins": card.snapshot["independent_origins"],
                "sources": card.snapshot["sources"],
            }
            for card in daily_cards
        ]
        if not chosen:
            return {"period": day, "cards": []}
        h = digest(chosen)
        existing = s.scalar(
            select(Digest).where(Digest.product_id == p, Digest.period == day, Digest.content_hash == h)
        )
        if not existing:
            s.add(
                Digest(product_id=p, period=day, snapshot=chosen, content_hash=h, created_at=app.clock.now())
            )
        return {"period": day, "cards": chosen}


def record_product_decision(app, actor, p, card_id, request):
    actor.require(p, "product_reviewer")
    with app.db.tx([p], actor.id) as s:
        s.scalar(
            select(ProductLease)
            .where(ProductLease.product_id == p, ProductLease.id == "assignment")
            .with_for_update()
        )
        c = s.scalar(select(Card).where(Card.product_id == p, Card.id == card_id).with_for_update())
        if not c:
            raise NotFound()
        if (
            c.revision != request.revision
            or c.content_hash != request.content_hash
            or c.status in ("stale", "correction_required")
            or not c.snapshot
        ):
            raise Conflict()
        previous = s.scalar(
            select(Decision).where(
                Decision.product_id == p, Decision.card_id == c.id, Decision.revision == c.revision
            )
        )
        if previous:
            if previous.decision == request.decision:
                if request.decision == "accepted" and app.settings.delivery_mode == "reviewed":
                    out = s.scalar(
                        select(Outbox)
                        .where(Outbox.product_id == p, Outbox.card_id == c.id, Outbox.revision == c.revision)
                        .with_for_update()
                    )
                    if out and out.status == "awaiting_review":
                        # An operator confirmed absence at the receiver; only a fresh reviewer action resumes.
                        previous.actor, previous.reason, previous.created_at = (
                            actor.id,
                            mask(request.reason, p),
                            app.clock.now(),
                        )
                        c.status, out.status = "accepted", "pending"
                        _reset_delivery_job(s, app, p, out)
                        audit(s, p, "card_reapproved", c.id, app.clock.now(), actor.id)
                    else:
                        prepare_outbox(s, app, p, c, previous)
                return {"decision_id": previous.id, "status": c.status}
            raise Conflict()
        decision = Decision(
            product_id=p,
            card_id=c.id,
            revision=c.revision,
            content_hash=c.content_hash,
            actor=actor.id,
            decision=request.decision,
            reason=mask(request.reason, p),
            created_at=app.clock.now(),
        )
        s.add(decision)
        s.flush()
        c.status = request.decision
        if request.decision == "accepted" and app.settings.delivery_mode == "reviewed":
            prepare_outbox(s, app, p, c, decision)
        audit(s, p, f"card_{request.decision}", c.id, app.clock.now(), actor.id)
        return {"decision_id": decision.id, "status": c.status}


def prepare_outbox(s, app, p, card, decision):
    key = f"{p}:{card.id}:{card.revision}"
    existing = s.scalar(select(Outbox).where(Outbox.product_id == p, Outbox.correlation_key == key))
    if existing:
        return existing
    out = Outbox(
        product_id=p,
        card_id=card.id,
        decision_id=decision.id,
        revision=card.revision,
        content_hash=card.content_hash,
        correlation_key=key,
        status="pending",
    )
    s.add(out)
    s.flush()
    enqueue(s, p, "delivery", f"delivery:{out.id}", app.clock.now())
    return out


def approval_is_current(s, p, outbox, card):
    if (
        not card
        or not card.snapshot
        or card.revision != outbox.revision
        or card.content_hash != outbox.content_hash
        or digest(card.snapshot) != card.content_hash
        or card.status != "accepted"
    ):
        return False
    decision = s.get(Decision, (p, outbox.decision_id))
    if (
        not decision
        or decision.decision != "accepted"
        or decision.revision != outbox.revision
        or decision.content_hash != outbox.content_hash
    ):
        return False
    return bool(
        s.scalar(
            select(Grant.id)
            .join(Principal, Principal.id == Grant.actor)
            .where(
                Grant.product_id == p,
                Grant.actor == decision.actor,
                Grant.role == "product_reviewer",
                Principal.enabled.is_(True),
            )
        )
    )


def _reset_delivery_job(s, app, p, outbox):
    key = f"delivery:{outbox.id}"
    job = s.scalar(select(Job).where(Job.product_id == p, Job.dedupe_key == key).with_for_update())
    if job:
        if job.status == "running" and job.lease_until and job.lease_until > app.clock.now():
            raise Conflict()
        job.status, job.attempts, job.available_at = "queued", 0, app.clock.now()
        job.claim_token, job.lease_until, job.error_code = None, None, None
    else:
        enqueue(s, p, "delivery", key, app.clock.now())


def reconcile_delivery(app, actor, p, outbox_id, resolution, reason, external_id=None):
    """Record explicit receiver reconciliation; an operator cannot approve a resend."""
    actor.require(p, "operator")
    reason = mask(reason.strip(), p)
    if not reason or len(reason) > 500 or resolution not in ("delivered", "not_delivered", "corrected"):
        raise Permanent()
    if resolution == "delivered" and (not external_id or len(external_id) > 100):
        raise Permanent()
    if resolution == "not_delivered" and external_id:
        raise Permanent()
    with app.db.tx([p], actor.id) as s:
        s.scalar(
            select(ProductLease)
            .where(ProductLease.product_id == p, ProductLease.id == "assignment")
            .with_for_update()
        )
        out = s.scalar(select(Outbox).where(Outbox.product_id == p, Outbox.id == outbox_id).with_for_update())
        if not out:
            raise NotFound()
        card = s.scalar(select(Card).where(Card.product_id == p, Card.id == out.card_id).with_for_update())
        if resolution == "corrected":
            if out.status != "correction_required":
                raise Conflict()
            out.status = "corrected"
            if card and card.revision == out.revision and card.status == "correction_required":
                card.status = "stale"
        else:
            if out.status != "delivery_unknown":
                raise Conflict()
            if resolution == "delivered":
                current = approval_is_current(s, p, out, card)
                out.external_id, out.sent_at = external_id, app.clock.now()
                out.status = "delivered" if current else "correction_required"
                if card and card.revision == out.revision:
                    card.status = out.status
            else:
                same_snapshot = (
                    card
                    and card.snapshot
                    and card.revision == out.revision
                    and card.content_hash == out.content_hash
                    and digest(card.snapshot) == card.content_hash
                    and card.status not in ("stale", "correction_required")
                )
                out.status = "awaiting_review" if same_snapshot else "cancelled"
                if same_snapshot:
                    card.status = "needs_clarification"
        out.resolution_reason, out.resolved_by, out.resolved_at = reason, actor.id, app.clock.now()
        audit(s, p, f"delivery_resolved_{resolution}", out.id, app.clock.now(), actor.id)
        return {
            "outbox_id": out.id,
            "status": out.status,
            "requires_reapproval": out.status == "awaiting_review",
        }
