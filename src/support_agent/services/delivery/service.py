from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select, case
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
)
from support_agent.policy import digest, mask
from support_agent.queue import enqueue
from support_agent.errors import Conflict, NotFound
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
                .where(
                    Need.product_id == p,
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
                "known_backlog": None,
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
        # The daily snapshot retains earlier valid cards as new ones are added or revised.
        chosen = [
            {
                "card_id": c.id,
                "revision": c.revision,
                "content_hash": c.content_hash,
                "kind": c.snapshot["kind"],
                "need": c.snapshot["need"],
                "independent_origins": c.snapshot["independent_origins"],
                "sources": c.snapshot["sources"],
            }
            for c in s.scalars(
                select(Card)
                .where(
                    Card.product_id == p,
                    Card.id.in_(prepared),
                    Card.status.not_in(["stale", "correction_required"]),
                )
                .order_by(
                    case((Card.snapshot["kind"].astext == "feature_request", 0), else_=1),
                    Card.snapshot["independent_origins"].as_integer().desc(),
                    Card.id,
                )
            )
            if c.snapshot
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
