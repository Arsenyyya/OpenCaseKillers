import json
from datetime import timedelta
from sqlalchemy import select, func
from support_agent.db.models import Job, Revision, Need, Card, MediaCache, Ticket, Outbox, Receipt
from support_agent.queue import enqueue
from support_agent.worker import run_one, drain
from support_agent.errors import Transient, Permanent
from support_agent.services.ingest.service import ingest_once
from support_agent.services.delivery.service import build_digest, record_product_decision
from support_agent.services.state import retention_preview, retention_run
from support_agent.auth import authenticate
from support_agent.contracts import DecisionRequest


async def normalize_all(app):
    await ingest_once(app, "atlas")
    while await run_one(app, "media"):
        pass


async def test_bounded_retry_permanent_and_no_content_in_logs(app, caplog):
    with app.db.tx(["atlas"]) as s:
        enqueue(s, "atlas", "probe", "transient", app.clock.now())
        enqueue(s, "atlas", "probe", "permanent", app.clock.now())
    c = app.queue.claim("probe")
    app.queue.fail(c, Transient(retry_after=30))
    with app.db.tx(["atlas"]) as s:
        j = s.get(Job, ("atlas", c.id))
        assert j.status == "retry_wait" and j.available_at >= app.clock.now() + timedelta(seconds=30)
    c2 = app.queue.claim("probe")
    app.queue.fail(c2, Permanent("DO_NOT_LOG_SECRET"))
    with app.db.tx(["atlas"]) as s:
        assert s.get(Job, ("atlas", c2.id)).status == "dead"
    for _ in range(2):
        app.clock.value += timedelta(seconds=100)
        c = app.queue.claim("probe")
        app.queue.fail(c, Transient())
    app.clock.value += timedelta(seconds=100)
    assert app.queue.claim("probe") is None
    with app.db.tx(["atlas"]) as s:
        j = s.get(Job, ("atlas", c.id))
        assert j.status == "dead" and j.attempts == 3
    assert "DO_NOT_LOG_SECRET" not in caplog.text


async def test_invalid_model_output_repairs_once_then_dead_others_continue(app, caplog):
    await normalize_all(app)
    original = app.models.extract
    calls = {"invalid": 0}

    async def invalid(segments, repair=False):
        if "Хочу выгружать" in segments[0]["text"]:
            calls["invalid"] += 1
            return '{"items":[{"raw_secret":"DO_NOT_LOG_SECRET"}]}', {}
        return await original(segments, repair)

    app.models.extract = invalid
    await drain(app)
    with app.db.tx(["atlas"]) as s:
        j = s.scalar(select(Job).where(Job.kind == "analysis", Job.revision_id == "demo:t01:1"))
        assert j.status == "dead" and j.error_code == "invalid_model_output"
        assert not s.scalars(select(Need).where(Need.revision_id == "demo:t01:1")).all()
        assert s.get(Revision, ("atlas", "demo:t02:1")).status == "processed"
        assert s.get(Revision, ("atlas", "demo:t01:1")).model_calls == 2
    assert "DO_NOT_LOG_SECRET" not in caplog.text
    assert "demo.person@example.test" not in caplog.text
    assert calls["invalid"] == 6  # three independent fixture tickets share the attacked text


async def test_persisted_model_budget_stops_retries_and_capacity_returns_slot(app):
    await normalize_all(app)
    with app.db.tx(["atlas"]) as s:
        r = s.get(Revision, ("atlas", "demo:t01:1"))
        r.model_calls = app.settings.max_model_calls
    await drain(app)
    with app.db.tx(["atlas"]) as s:
        j = s.scalar(select(Job).where(Job.kind == "analysis", Job.revision_id == "demo:t01:1"))
        assert j.status == "dead" and j.error_code == "model_budget_exhausted"
        assert s.get(Revision, ("atlas", "demo:t01:1")).model_calls == app.settings.max_model_calls
    slots = [app.queue.acquire_slot("llm") for _ in range(app.settings.model_slots)]
    for slot in slots:
        app.queue.slot_action(*slot, release=True)


async def test_retention_dry_run_and_verified_purge(app):
    await ingest_once(app, "atlas")
    await drain(app)
    build_digest(app, "atlas")
    with app.db.tx(["atlas"]) as s:
        assert retention_preview(s, "atlas", app.clock.now(), 90) == []
        assert s.get(Revision, ("atlas", "demo:t21:1")).segments
    app.clock.value += timedelta(days=91)
    with app.db.tx(["atlas"]) as s:
        preview = retention_preview(s, "atlas", app.clock.now(), 90)
        assert len(preview) == 24
        assert s.get(Revision, ("atlas", "demo:t21:1")).raw
    with app.db.tx(["atlas"]) as s:
        assert retention_run(s, "atlas", app.clock.now(), 90) == preview
    with app.db.tx(["atlas"]) as s:
        assert s.scalar(select(func.count()).select_from(Need)) == 0
        assert s.scalar(select(func.count()).select_from(MediaCache)) == 0
        assert all(r.raw is None and r.segments is None for r in s.scalars(select(Revision)))
        assert all(t.deleted for t in s.scalars(select(Ticket)))
        assert all(c.snapshot == {} for c in s.scalars(select(Card)))
        assert retention_preview(s, "atlas", app.clock.now(), 90) == []


async def test_timeout_after_local_delivery_does_not_duplicate(app):
    await ingest_once(app, "atlas")
    await drain(app)
    build_digest(app, "atlas")
    with app.db.tx(["atlas"]) as s:
        c = s.scalar(select(Card).order_by(Card.id).limit(1))
    actor = authenticate(app, "Bearer " + app.tokens["reviewer"])
    record_product_decision(
        app,
        actor,
        "atlas",
        c.id,
        DecisionRequest(revision=c.revision, content_hash=c.content_hash, decision="accepted", reason="test"),
    )
    original = app.local_deliver
    first = True

    async def timeout(p, key, payload):
        nonlocal first
        external = await original(p, key, payload)
        if first:
            first = False
            raise Transient()
        return external

    app.local_deliver = timeout
    await run_one(app, "delivery")
    app.clock.value += timedelta(seconds=10)
    await run_one(app, "delivery")
    with app.db.tx(["atlas"]) as s:
        assert s.scalar(select(func.count()).select_from(Receipt)) == 1
        assert s.scalar(select(Outbox.status)) == "delivered"


async def test_source_change_after_remote_write_requires_correction(app, tmp_path):
    await ingest_once(app, "atlas")
    await drain(app)
    build_digest(app, "atlas")
    with app.db.tx(["atlas"]) as s:
        c = s.scalar(
            select(Card).where(Card.snapshot["need"].astext == "Добавить выгрузку списка заказов в CSV")
        )
    actor = authenticate(app, "Bearer " + app.tokens["reviewer"])
    record_product_decision(
        app,
        actor,
        "atlas",
        c.id,
        DecisionRequest(revision=c.revision, content_hash=c.content_hash, decision="accepted", reason="test"),
    )
    original = app.local_deliver

    async def change_after_write(p, key, payload):
        external = await original(p, key, payload)
        import shutil

        root = tmp_path / "fixtures"
        shutil.copytree(app.settings.fixtures_dir, root)
        path = root / "atlas/changes.jsonl"
        path.write_text(path.read_text() + (root / "atlas/mutations.jsonl").read_text())
        app.settings = app.settings.model_copy(update={"fixtures_dir": root})
        await ingest_once(app, "atlas")
        return external

    app.local_deliver = change_after_write
    await run_one(app, "delivery")
    with app.db.tx(["atlas"]) as s:
        assert s.scalar(select(Outbox.status)) == "correction_required"
        assert s.get(Card, ("atlas", c.id)).status == "correction_required"


async def test_reviewer_revocation_blocks_pending_delivery(app):
    from support_agent.db.models import Grant

    await ingest_once(app, "atlas")
    await drain(app)
    build_digest(app, "atlas")
    with app.db.tx(["atlas"]) as s:
        c = s.scalar(select(Card).order_by(Card.id).limit(1))
    actor = authenticate(app, "Bearer " + app.tokens["reviewer"])
    record_product_decision(
        app,
        actor,
        "atlas",
        c.id,
        DecisionRequest(revision=c.revision, content_hash=c.content_hash, decision="accepted", reason="test"),
    )
    with app.db.tx(["atlas"]) as s:
        binding = s.scalar(select(Grant).where(Grant.actor == "reviewer"))
        s.delete(binding)
    await drain(app)
    with app.db.tx(["atlas"]) as s:
        assert s.scalar(select(Outbox.status)) == "cancelled"
        assert s.scalar(select(func.count()).select_from(Receipt)) == 0


async def test_reserved_text_queue_progresses_while_media_is_leased(app):
    await ingest_once(app, "atlas")
    with app.db.tx(["atlas"]) as s:
        for j in s.scalars(select(Job).where(Job.kind == "media_text")):
            j.status = "cancelled"
    media = app.queue.claim("media")
    assert media is not None
    with app.db.tx(["atlas"]) as s:
        assert s.get(Job, ("atlas", media.id)).kind == "media"
    with app.db.tx(["atlas"]) as s:
        j = s.scalar(select(Job).where(Job.revision_id == "demo:t01:1"))
        j.status = "queued"
    text_claim = app.queue.claim("media_text")
    assert text_claim and text_claim.revision_id == "demo:t01:1"
    from support_agent.services.media.worker import run_media

    await run_media(app, text_claim)
    with app.db.tx(["atlas"]) as s:
        assert s.get(Job, ("atlas", media.id)).status == "running"
        assert s.get(Revision, ("atlas", "demo:t01:1")).segments


async def test_long_ticket_last_segment_is_covered_and_matching_is_bounded(app):
    await ingest_once(app, "atlas")
    tail = "Хочу CSV для списка заказов"
    with app.db.tx(["atlas"]) as s:
        for j in s.scalars(select(Job).where(Job.revision_id != "demo:t01:1")):
            j.status = "cancelled"
        r = s.get(Revision, ("atlas", "demo:t01:1"))
        r.raw = dict(r.raw, text="demo.person@example.test " + ("x" * 17000) + "\n" + tail)

    class ControlledLongInputProvider:
        model_id = "synthetic-long-input-test"
        extracts = 0

        async def extract(self, segments, repair=False):
            self.extracts += 1
            assert sum(len(s["text"]) for s in segments) <= app.settings.context_chars
            assert all("demo.person@example.test" not in s["text"] for s in segments)
            found = next((s for s in segments if tail in s["text"]), None)
            item = (
                {
                    "kind": "feature_request",
                    "urgency": "unknown",
                    "need": "Добавить CSV",
                    "context": "",
                    "evidence": [{"segment_id": found["segment_id"], "quote": tail}],
                    "needs_review": False,
                    "review_reason": None,
                }
                if found
                else None
            )
            return json.dumps({"items": [item] if item else []}), {}

        async def match(self, item, candidates, segments):
            assert sum(len(s["text"]) for s in segments) <= 1500
            return json.dumps(
                {
                    "action": "new",
                    "candidate_id": None,
                    "reason": "synthetic",
                    "evidence": [e.model_dump() for e in item.evidence],
                }
            ), {}

    app.models = ControlledLongInputProvider()
    await drain(app)
    with app.db.tx(["atlas"]) as s:
        r = s.get(Revision, ("atlas", "demo:t01:1"))
        assert r.status == "processed" and r.coverage["complete"] and r.model_calls == 3
        need = s.scalar(select(Need).where(Need.revision_id == r.id))
        assert need.evidence[0]["quote"] == tail and need.evidence[0]["segment_id"].endswith("part2")
    assert app.models.extracts == 2
