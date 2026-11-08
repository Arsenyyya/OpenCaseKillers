import json
import os
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert
from support_agent.db.models import (
    Product,
    Principal,
    Grant,
    Cursor,
    ProductLease,
    ProviderSlot,
    Card,
    Membership,
    Receipt,
    Ticket,
)
from support_agent.db.session import Database
from support_agent.auth import token_hash, authenticate
from support_agent.contracts import DecisionRequest
from support_agent.services.ingest.service import ingest_once
from support_agent.services.delivery.service import build_digest, record_product_decision
from support_agent.worker import drain
from support_agent.observability.metrics import report


def seed_demo(app):
    if app.settings.app_env != "local" or app.settings.mode != "demo":
        raise RuntimeError("seed-demo is local demo only")
    path = app.settings.token_file
    if not path.exists():
        from secrets import token_urlsafe

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({n: token_urlsafe(32) for n in ["viewer", "reviewer", "operator", "boreal"]})
        )
        os.chmod(path, 0o600)
    tokens = json.loads(path.read_text())
    admin = Database(app.settings.database_admin_url)
    try:
        with admin.tx() as s:
            for p, owner in [("atlas", "reviewer"), ("boreal", "boreal")]:
                s.execute(
                    insert(Product)
                    .values(id=p, owner=owner, terms={"CSV": "табличная выгрузка заказов"})
                    .on_conflict_do_nothing()
                )
                for source in ("demo", "gateway"):
                    s.execute(
                        insert(Cursor)
                        .values(product_id=p, id=source, position="0", initialized_at=app.clock.now())
                        .on_conflict_do_nothing()
                    )
                s.execute(insert(ProductLease).values(product_id=p, id="assignment").on_conflict_do_nothing())
            for actor, token in tokens.items():
                s.execute(
                    insert(Principal)
                    .values(id=actor, token_hash=token_hash(token), enabled=True)
                    .on_conflict_do_update(
                        index_elements=["id"], set_={"token_hash": token_hash(token), "enabled": True}
                    )
                )
                products = (
                    ["atlas", "boreal"]
                    if actor == "operator"
                    else ["boreal" if actor == "boreal" else "atlas"]
                )
                role = {
                    "viewer": "viewer",
                    "reviewer": "product_reviewer",
                    "operator": "operator",
                    "boreal": "product_reviewer",
                }[actor]
                for p in products:
                    s.execute(
                        insert(Grant)
                        .values(product_id=p, id=f"{actor}:{role}", actor=actor, role=role)
                        .on_conflict_do_nothing()
                    )
            for provider in ["llm", "asr", "vl"]:
                for idx in range(app.settings.model_slots):
                    s.execute(
                        insert(ProviderSlot)
                        .values(id=f"{provider}:{idx}", provider=provider)
                        .on_conflict_do_nothing()
                    )
    finally:
        admin.close()
    return tokens


async def run_demo(app):
    tokens = seed_demo(app)
    for p in app.settings.service_products:
        await ingest_once(app, p)
    jobs = await drain(app)
    snapshots = {}
    for p in app.settings.service_products:
        result = build_digest(app, p)
        snapshots[p] = result
        actor = authenticate(app, "Bearer " + tokens["reviewer" if p == "atlas" else "boreal"])
        with app.db.tx([p]) as s:
            card = s.scalar(
                select(Card)
                .where(
                    Card.status.in_(["draft", "accepted"]),
                    Card.snapshot["need"].astext == "Добавить выгрузку списка заказов в CSV",
                )
                .order_by(Card.id)
                .limit(1)
            )
        if card:
            record_product_decision(
                app,
                actor,
                p,
                card.id,
                DecisionRequest(
                    revision=card.revision,
                    content_hash=card.content_hash,
                    decision="accepted",
                    reason="Synthetic demo approval",
                ),
            )
    await drain(app)

    def counts():
        with app.db.tx(app.settings.service_products) as s:
            return {
                name: s.scalar(select(func.count()).select_from(cls))
                for name, cls in [
                    ("tickets", Ticket),
                    ("memberships", Membership),
                    ("cards", Card),
                    ("receipts", Receipt),
                ]
            }

    before = counts()
    for p in app.settings.service_products:
        await ingest_once(app, p)
        build_digest(app, p)
    await drain(app)
    after = counts()
    if before != after:
        raise RuntimeError("demo idempotency invariant failed")
    return {
        "synthetic": True,
        "delivery_mode": app.settings.delivery_mode,
        "jobs_processed": jobs,
        "counts": after,
        "repeat_unchanged": before == after,
        "digests": snapshots,
        "metrics": report(app, app.settings.service_products),
        "model_quality": "not run; fixture provider",
    }
