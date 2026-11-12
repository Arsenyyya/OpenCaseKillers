from sqlalchemy import select

from support_agent.db.models import Card, Outbox, ProductLease
from support_agent.errors import DeliveryUnknown, Permanent
from support_agent.services.delivery.service import approval_is_current
from support_agent.services.state import audit


def _lock_product(s, p):
    s.scalar(
        select(ProductLease)
        .where(ProductLease.product_id == p, ProductLease.id == "assignment")
        .with_for_update()
    )


async def run_delivery(app, c):
    p = c.product_id
    with app.db.tx([p]) as s:
        _lock_product(s, p)
        j = app.queue.guard(s, c)
        oid = j.dedupe_key.removeprefix("delivery:")
        outbox = s.scalar(select(Outbox).where(Outbox.product_id == p, Outbox.id == oid).with_for_update())
        if not outbox or outbox.status in (
            "cancelled",
            "delivery_unknown",
            "correction_required",
            "delivered",
            "awaiting_review",
            "corrected",
        ):
            app.queue.complete(s, c)
            return
        card = s.get(Card, (p, outbox.card_id))
        if app.settings.delivery_mode != "reviewed" or not approval_is_current(s, p, outbox, card):
            outbox.status = "cancelled"
            app.queue.complete(s, c)
            return
        if (
            outbox.status == "sending"
            and app.gateway
            and not (app.settings.backlog_idempotent or app.settings.backlog_lookup)
        ):
            outbox.status = "delivery_unknown"
            audit(s, p, "delivery_unknown", outbox.id, app.clock.now())
            app.queue.complete(s, c)
            return
        outbox.status = "sending"
        key, payload = outbox.correlation_key, card.snapshot
    try:
        external = (
            await app.gateway.deliver(p, key, payload)
            if app.gateway
            else await app.local_deliver(p, key, payload)
        )
    except DeliveryUnknown:
        with app.db.tx([p]) as s:
            _lock_product(s, p)
            app.queue.guard(s, c)
            s.get(Outbox, (p, oid)).status = "delivery_unknown"
            audit(s, p, "delivery_unknown", oid, app.clock.now())
            app.queue.complete(s, c)
        return
    except Permanent:
        # A definitive gateway rejection is a known failure, unlike an interrupted successful POST.
        with app.db.tx([p]) as s:
            _lock_product(s, p)
            app.queue.guard(s, c)
            s.get(Outbox, (p, oid)).status = "failed"
            audit(s, p, "delivery_failed", oid, app.clock.now())
        raise
    with app.db.tx([p]) as s:
        _lock_product(s, p)
        app.queue.guard(s, c)
        outbox = s.scalar(select(Outbox).where(Outbox.product_id == p, Outbox.id == oid).with_for_update())
        card = s.get(Card, (p, outbox.card_id))
        # Source deletion/revision changes and reviewer revocation can race the external request.
        current = outbox.status != "cancelled" and approval_is_current(s, p, outbox, card)
        outbox.external_id, outbox.sent_at = external, app.clock.now()
        outbox.status = "delivered" if current else "correction_required"
        if card and card.revision == outbox.revision:
            card.status = outbox.status
        audit(s, p, outbox.status, oid, app.clock.now())
        app.queue.complete(s, c)
