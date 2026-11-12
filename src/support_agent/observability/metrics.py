"""Bounded aggregate metrics; source content and per-ticket identifiers never leave the database."""

from sqlalchemy import and_, select, func
from support_agent.db.models import Ticket, Revision, Job, Metric, Card, Outbox, Decision, Audit

PROVIDER_METRICS = (
    "llm_calls",
    "asr_calls",
    "vl_calls",
    "asr_minutes",
    "vl_pages",
    "invalid_json",
    "forbidden_operations",
    "dedupe_hits",
)


def percentile(values, fraction):
    """Compatibility helper for local callers; persisted metrics use PostgreSQL percentiles."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def _percentiles(session, expression, source, condition=None):
    statement = select(
        func.percentile_cont(0.5).within_group(expression),
        func.percentile_cont(0.95).within_group(expression),
    ).select_from(source)
    if condition is not None:
        statement = statement.where(condition)
    p50, p95 = session.execute(statement).one()
    return {"p50": p50, "p95": p95}


def report(app, products):
    now = app.clock.now()
    with app.db.tx(products) as s:
        current = and_(
            Revision.product_id == Ticket.product_id,
            Revision.ticket_id == Ticket.id,
            Revision.number == Ticket.current_revision,
        )
        source = Ticket.__table__.outerjoin(Revision.__table__, current)
        rows = s.execute(
            select(
                Ticket.channel,
                func.count(Ticket.id).label("available_in_ingested_scope"),
                func.count(Revision.id).label("accepted"),
                func.count()
                .filter(
                    and_(Revision.status == "processed", Revision.coverage["complete"].as_boolean().is_(True))
                )
                .label("fully_processed"),
                func.count().filter(Revision.status == "partial").label("partial"),
                func.count().filter(Revision.status == "dead").label("dead"),
                func.count().filter(Revision.status == "needs_review").label("needs_review"),
            )
            .select_from(source)
            .where(Ticket.deleted.is_(False))
            .group_by(Ticket.channel)
        ).mappings()
        channels = {}
        for row in rows:
            entry = dict(row)
            name = entry.pop("channel")
            entry["coverage"] = entry["fully_processed"] / entry["available_in_ingested_scope"]
            channels[name] = entry
        elapsed = func.extract("epoch", Revision.processed_at - Revision.created_at)
        latency = _percentiles(
            s, elapsed, source, and_(Ticket.deleted.is_(False), Revision.processed_at.is_not(None))
        )
        jobs = dict(s.execute(select(Job.status, func.count()).group_by(Job.status)).all())
        waiting = Job.status.in_(["queued", "retry_wait", "running"])
        count, oldest = s.execute(select(func.count(), func.min(Job.created_at)).where(waiting)).one()
        attempts, retries = s.execute(
            select(func.sum(Job.attempts), func.sum(func.greatest(Job.attempts - 1, 0)))
        ).one()
        counters = {name: 0 for name in PROVIDER_METRICS}
        counters.update(
            dict(
                s.execute(
                    select(Metric.name, func.sum(Metric.value))
                    .where(Metric.name.in_(PROVIDER_METRICS))
                    .group_by(Metric.name)
                ).all()
            )
        )
        tokens = {
            key: s.scalar(select(func.sum(Revision.usage[key].as_integer())))
            for key in ("input_tokens", "output_tokens")
        }
        cards = dict(s.execute(select(Card.status, func.count()).group_by(Card.status)).all())
        viewed, unviewed = s.execute(
            select(
                func.count().filter(Card.viewed_at.is_not(None)),
                func.count().filter(Card.viewed_at.is_(None)),
            )
        ).one()
        delivery = dict(s.execute(select(Outbox.status, func.count()).group_by(Outbox.status)).all())
        decision_source = Decision.__table__.join(
            Card.__table__, and_(Decision.product_id == Card.product_id, Decision.card_id == Card.id)
        )
        decision_latency = _percentiles(
            s, func.extract("epoch", Decision.created_at - Card.created_at), decision_source
        )
        delivery_source = Outbox.__table__.join(
            Card.__table__, and_(Outbox.product_id == Card.product_id, Outbox.card_id == Card.id)
        )
        delivery_latency = _percentiles(
            s,
            func.extract("epoch", Outbox.sent_at - Card.created_at),
            delivery_source,
            Outbox.sent_at.is_not(None),
        )
        prepared = s.scalar(select(func.count()).select_from(Audit).where(Audit.event.like("card_prepared%")))
        return {
            "channels": channels,
            "source_total_available": None,
            "source_total_note": "Denominator is the entire persisted agreed intake scope, including partial/dead; source-wide total requires gateway counters.",
            "jobs": jobs,
            "queue_length": count,
            "oldest_job_seconds": max(0, (now - oldest).total_seconds()) if oldest else 0,
            "processing_latency_seconds": latency,
            "retry_rate": (retries or 0) / max(1, attempts or 0),
            "provider": counters,
            "reported_tokens": tokens,
            "cards": cards,
            "prepared_snapshots": prepared,
            "viewed_cards": viewed,
            "unviewed_cards": unviewed,
            "delivery": delivery,
            "decision_latency_seconds_p50": decision_latency["p50"],
            "delivery_latency_seconds_p50": delivery_latency["p50"],
            "decision_latency_seconds": decision_latency,
            "delivery_latency_seconds": delivery_latency,
            "card_latency_reference": "First preparation of the stable card; not ticket closure time or manual labor.",
        }
