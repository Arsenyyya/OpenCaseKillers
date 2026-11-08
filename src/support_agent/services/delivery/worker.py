from sqlalchemy import select
from support_agent.db.models import Outbox, Card, Decision, Grant, Principal, ProductLease
from support_agent.errors import DeliveryUnknown
from support_agent.services.state import audit


async def run_delivery(app, c):
    p = c.product_id
    with app.db.tx([p]) as s:
        s.scalar(
            select(ProductLease)
            .where(ProductLease.product_id == p, ProductLease.id == "assignment")
            .with_for_update()
        )
        j = app.queue.guard(s, c)
        oid = j.dedupe_key.removeprefix("delivery:")
        o = s.scalar(select(Outbox).where(Outbox.product_id == p, Outbox.id == oid).with_for_update())
        if not o or o.status in ("cancelled", "delivery_unknown", "correction_required", "delivered"):
            app.queue.complete(s, c)
            return
        card = s.get(Card, (p, o.card_id))
        decision = s.get(Decision, (p, o.decision_id))
        authorized = s.scalar(
            select(Grant.id)
            .join(Principal, Principal.id == Grant.actor)
            .where(
                Grant.product_id == p,
                Grant.actor == decision.actor,
                Grant.role == "product_reviewer",
                Principal.enabled.is_(True),
            )
        )
        if (
            not authorized
            or not card.snapshot
            or app.settings.delivery_mode != "reviewed"
            or card.revision != o.revision
            or (card.content_hash != o.content_hash)
            or card.status != "accepted"
            or (decision.decision != "accepted")
            or decision.content_hash != o.content_hash
        ):
            o.status = "cancelled"
            app.queue.complete(s, c)
            return
        if (
            o.status == "sending"
            and app.gateway
            and not (app.settings.backlog_idempotent or app.settings.backlog_lookup)
        ):
            o.status = "delivery_unknown"
            app.queue.complete(s, c)
            return
        o.status = "sending"
        key, payload = o.correlation_key, card.snapshot
    try:
        external = (
            await app.gateway.deliver(p, key, payload)
            if app.gateway
            else await app.local_deliver(p, key, payload)
        )
    except DeliveryUnknown:
        with app.db.tx([p]) as s:
            s.scalar(
                select(ProductLease)
                .where(ProductLease.product_id == p, ProductLease.id == "assignment")
                .with_for_update()
            )
            app.queue.guard(s, c)
            s.get(Outbox, (p, oid)).status = "delivery_unknown"
            audit(s, p, "delivery_unknown", oid, app.clock.now())
            app.queue.complete(s, c)
        return
    with app.db.tx([p]) as s:
        s.scalar(
            select(ProductLease)
            .where(ProductLease.product_id == p, ProductLease.id == "assignment")
            .with_for_update()
        )
        app.queue.guard(s, c)
        o = s.scalar(select(Outbox).where(Outbox.product_id == p, Outbox.id == oid).with_for_update())
        card = s.get(Card, (p, o.card_id))
        stale = o.status == "cancelled" or card.revision != o.revision or card.content_hash != o.content_hash
        o.external_id, o.sent_at = external, app.clock.now()
        o.status = "correction_required" if stale else "delivered"
        if card.revision == o.revision:
            card.status = "correction_required" if stale else "delivered"
        audit(s, p, o.status, oid, app.clock.now())
        app.queue.complete(s, c)
