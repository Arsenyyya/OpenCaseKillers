from collections import Counter
from sqlalchemy import select
from support_agent.db.models import Ticket, Revision, Job, Metric, Card, Outbox, Decision


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * fraction))]


def report(app, products):
    now = app.clock.now()
    with app.db.tx(products) as s:
        tickets = s.scalars(select(Ticket).where(Ticket.deleted.is_(False))).all()
        revisions = {r.id: r for r in s.scalars(select(Revision))}
        channel = {}
        latencies = []
        for t in tickets:
            r = revisions.get(f"{t.id}:{t.current_revision}")
            ch = channel.setdefault(
                t.channel,
                {
                    "available_in_ingested_scope": 0,
                    "accepted": 0,
                    "fully_processed": 0,
                    "partial": 0,
                    "dead": 0,
                    "needs_review": 0,
                },
            )
            ch["available_in_ingested_scope"] += 1
            if r:
                ch["accepted"] += 1
                if r.status == "processed" and r.coverage.get("complete"):
                    ch["fully_processed"] += 1
                if r.status in ("partial", "dead", "needs_review"):
                    ch[r.status] += 1
                if r.processed_at:
                    latencies.append((r.processed_at - r.created_at).total_seconds())
        for ch in channel.values():
            ch["coverage"] = ch["fully_processed"] / ch["available_in_ingested_scope"]
        jobs = s.scalars(select(Job)).all()
        waiting = [j for j in jobs if j.status in ("queued", "retry_wait", "running")]
        counters = Counter()
        for m in s.scalars(select(Metric)):
            counters[m.name] += m.value
        cards = s.scalars(select(Card)).all()
        outbox = s.scalars(select(Outbox)).all()
        decisions = s.scalars(select(Decision)).all()
        return {
            "channels": channel,
            "source_total_available": None,
            "source_total_note": "Coverage denominator is the persisted agreed intake scope; source-wide baseline requires gateway counters",
            "jobs": dict(Counter(j.status for j in jobs)),
            "queue_length": len(waiting),
            "oldest_job_seconds": max(
                [max(0, (now - j.created_at).total_seconds()) for j in waiting], default=0
            ),
            "processing_latency_seconds": {
                "p50": percentile(latencies, 0.5),
                "p95": percentile(latencies, 0.95),
            },
            "retry_rate": sum(max(0, j.attempts - 1) for j in jobs) / max(1, sum(j.attempts for j in jobs)),
            "provider": dict(counters),
            "reported_tokens": {
                key: sum(r.usage.get(key, 0) for r in revisions.values())
                if any(key in r.usage for r in revisions.values())
                else None
                for key in ("input_tokens", "output_tokens")
            },
            "cards": dict(Counter(c.status for c in cards)),
            "viewed_cards": sum(c.viewed_at is not None for c in cards),
            "unviewed_cards": sum(c.viewed_at is None for c in cards),
            "delivery": dict(Counter(o.status for o in outbox)),
            "decision_latency_seconds_p50": percentile(
                [
                    (d.created_at - c.created_at).total_seconds()
                    for d in decisions
                    for c in cards
                    if d.card_id == c.id
                ],
                0.5,
            ),
            "delivery_latency_seconds_p50": percentile(
                [
                    (o.sent_at - c.created_at).total_seconds()
                    for o in outbox
                    for c in cards
                    if o.card_id == c.id and o.sent_at
                ],
                0.5,
            ),
        }
