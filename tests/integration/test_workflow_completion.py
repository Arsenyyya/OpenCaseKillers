"""Regression cases beyond the first vertical demo: durable retries and manual recovery."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from support_agent.auth import authenticate
from support_agent.contracts import DecisionRequest
from support_agent.db.models import Card, Group, Job, Need, Outbox, Revision
from support_agent.errors import Conflict, DeliveryUnknown, NotFound, Permanent
from support_agent.services.delivery.service import build_digest, reconcile_delivery, record_product_decision
from support_agent.services.ingest.service import ingest_once
from support_agent.services.media.normalize import media_segments
from support_agent.worker import drain, run_one

CSV = "Добавить выгрузку списка заказов в CSV"


async def prepare_text(app, tickets=("demo:t01:1", "demo:t02:1")):
    await ingest_once(app, "atlas")
    with app.db.tx(["atlas"]) as s:
        for job in s.scalars(select(Job).where(Job.revision_id.not_in(tickets))):
            job.status = "cancelled"
    while await run_one(app, "media"):
        pass


def reviewer(app):
    return authenticate(app, "Bearer " + app.tokens["reviewer"])


def operator(app):
    return authenticate(app, "Bearer " + app.tokens["operator"])


async def prepare_approved(app):
    await prepare_text(app)
    await drain(app)
    result = build_digest(app, "atlas")
    card_id = result["cards"][0]["card_id"]
    with app.db.tx(["atlas"]) as s:
        card = s.get(Card, ("atlas", card_id))
        request = DecisionRequest(
            revision=card.revision,
            content_hash=card.content_hash,
            decision="accepted",
            reason="Synthetic approval",
        )
    record_product_decision(app, reviewer(app), "atlas", card.id, request)
    return card, request


async def test_assignment_capacity_reuses_durable_extraction(app):
    await prepare_text(app, tickets=("demo:t01:1",))
    extract = app.models.extract
    calls = 0

    async def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await extract(*args, **kwargs)

    app.models.extract = counted
    token = app.queue.acquire_product("atlas")
    await run_one(app, "analysis")
    app.clock.value += timedelta(seconds=3)
    await run_one(app, "analysis")
    with app.db.tx(["atlas"]) as s:
        revision = s.get(Revision, ("atlas", "demo:t01:1"))
        assert revision.model_calls == 1 and revision.extracted_items
        job = s.scalar(select(Job).where(Job.kind == "analysis", Job.revision_id == revision.id))
        assert job.status == "retry_wait" and job.attempts == 0
    app.queue.release_product("atlas", token)
    app.clock.value += timedelta(seconds=3)
    await run_one(app, "analysis")
    with app.db.tx(["atlas"]) as s:
        assert s.get(Revision, ("atlas", "demo:t01:1")).status == "processed"
        assert s.get(Revision, ("atlas", "demo:t01:1")).model_calls == 2
    assert calls == 1


async def test_duplicate_items_preserve_review_flag_and_all_distinct_evidence(app):
    await prepare_text(app, tickets=("demo:t01:1",))
    original = app.models.extract

    async def mixed(*args, **kwargs):
        raw, usage = await original(*args, **kwargs)
        payload = json.loads(raw)
        first = payload["items"][0]
        first["evidence"] = [{"segment_id": "body-1", "quote": "Хочу"}]
        second = dict(
            first,
            needs_review=True,
            review_reason="Нужно уточнить контекст",
            evidence=[{"segment_id": "body-1", "quote": "CSV"}],
        )
        return json.dumps({"items": [first, second]}), usage

    app.models.extract = mixed
    await run_one(app, "analysis")
    with app.db.tx(["atlas"]) as s:
        need = s.scalar(select(Need))
        assert need.needs_review and not need.eligible
        assert "уточнить" in need.review_reason
        assert {item["quote"] for item in need.evidence} == {"Хочу", "CSV"}
        assert s.get(Revision, ("atlas", "demo:t01:1")).status == "needs_review"
        group_count = s.scalar(select(func.count()).select_from(Group))
        calls = s.get(Revision, ("atlas", "demo:t01:1")).model_calls
        job = s.scalar(select(Job).where(Job.kind == "analysis"))
        job.status, job.attempts = "queued", 0
    await run_one(app, "analysis")
    with app.db.tx(["atlas"]) as s:
        assert s.scalar(select(func.count()).select_from(Group)) == group_count
        assert s.get(Revision, ("atlas", "demo:t01:1")).model_calls == calls
        assert s.scalar(select(func.count()).select_from(Need)) == 1


async def test_historical_digest_is_saved_snapshot_not_current_reconstruction(app):
    await prepare_text(app)
    await drain(app)
    first = build_digest(app, "atlas")
    app.clock.value += timedelta(days=1)
    assert build_digest(app, "atlas", period=first["period"]) == first
    with pytest.raises(NotFound):
        build_digest(app, "atlas", period="2026-11-04")
    with pytest.raises(Conflict):
        build_digest(app, "atlas", period="2026-11-09")


async def test_unknown_absent_delivery_requires_fresh_reviewer_action(app):
    card, request = await prepare_approved(app)

    async def unknown(*args):
        raise DeliveryUnknown()

    app.gateway = SimpleNamespace(deliver=unknown)
    await run_one(app, "delivery")
    with app.db.tx(["atlas"]) as s:
        outbox = s.scalar(select(Outbox))
        assert outbox.status == "delivery_unknown"
    result = reconcile_delivery(
        app, operator(app), "atlas", outbox.id, "not_delivered", "Получатель проверен вручную; записи нет"
    )
    assert result["status"] == "awaiting_review"
    assert not await run_one(app, "delivery")
    with app.db.tx(["atlas"]) as s:
        assert s.get(Card, ("atlas", card.id)).status == "needs_clarification"
    record_product_decision(app, reviewer(app), "atlas", card.id, request)
    app.gateway = None
    await run_one(app, "delivery")
    with app.db.tx(["atlas"]) as s:
        assert s.get(Outbox, ("atlas", outbox.id)).status == "delivered"
        assert s.get(Outbox, ("atlas", outbox.id)).resolved_by == "operator"
    with pytest.raises(Conflict):
        reconcile_delivery(app, operator(app), "atlas", outbox.id, "not_delivered", "duplicate resolution")


async def test_confirmed_old_delivery_requires_correction_not_resend(app):
    card, _ = await prepare_approved(app)
    with app.db.tx(["atlas"]) as s:
        outbox = s.scalar(select(Outbox))
        outbox.status = "delivery_unknown"
        current = s.get(Card, ("atlas", card.id))
        current.status = "stale"
    result = reconcile_delivery(
        app,
        operator(app),
        "atlas",
        outbox.id,
        "delivered",
        "Создание подтверждено получателем",
        external_id="confirmed-123",
    )
    assert result["status"] == "correction_required"
    acknowledged = reconcile_delivery(
        app, operator(app), "atlas", outbox.id, "corrected", "Старая карточка отозвана вручную"
    )
    assert acknowledged["status"] == "corrected"


async def test_permanent_receiver_rejection_has_known_failed_state(app):
    await prepare_approved(app)

    async def reject(*args):
        raise Permanent()

    app.gateway = SimpleNamespace(deliver=reject)
    await run_one(app, "delivery")
    app.gateway = None
    with app.db.tx(["atlas"]) as s:
        assert s.scalar(select(Outbox.status)) == "failed"
        job = s.scalar(select(Job).where(Job.kind == "delivery"))
        assert job.status == "dead" and job.error_code == "permanent_processing_error"


async def test_worker_heartbeat_failure_cancels_work_without_crashing_loop(app, monkeypatch):
    from support_agent.queue import enqueue
    from support_agent.worker import HANDLERS

    app.settings = app.settings.model_copy(update={"lease_seconds": 2})
    app.queue.settings = app.settings
    with app.db.tx(["atlas"]) as s:
        enqueue(s, "atlas", "heartbeat_probe", "heartbeat-probe", app.clock.now())
    finished = asyncio.Event()

    async def slow(app, claim):
        try:
            await asyncio.sleep(5)
            finished.set()
        finally:
            pass

    def unavailable(claim):
        raise RuntimeError("Never expose provider content here")

    monkeypatch.setitem(HANDLERS, "heartbeat_probe", slow)
    monkeypatch.setattr(app.queue, "heartbeat", unavailable)
    assert await run_one(app, "heartbeat_probe")
    assert not finished.is_set()
    with app.db.tx(["atlas"]) as s:
        assert s.scalar(select(Job.status)) == "retry_wait"


@pytest.mark.parametrize(
    "rows",
    [None, {}, "text", [], [{"text": "ok", "confidence": float("nan")}], [{"text": "ok", "confidence": 2.0}]],
)
def test_malformed_media_provider_output_is_permanent_not_retryable_crash(rows):
    with pytest.raises(Permanent):
        media_segments(rows, "image", "image.png", "image", "atlas")


async def test_semantically_matched_rephrase_preserves_first_observation(app, tmp_path):
    import shutil

    await prepare_text(app)
    await drain(app)
    with app.db.tx(["atlas"]) as s:
        previous = s.scalar(select(Need).where(Need.ticket_id == "demo:t01"))
        gid, first_seen = previous.group_id, previous.first_seen
    app.clock.value += timedelta(days=20)
    root = tmp_path / "rephrased"
    shutil.copytree(app.settings.fixtures_dir, root)
    payloads_file = root / "atlas/tickets.json"
    payloads = json.loads(payloads_file.read_text())
    payloads["t01:rephrased-v2"] = dict(
        payloads["t01:v1"], version="rephrased-v2", occurred_at=app.clock.now().isoformat()
    )
    payloads_file.write_text(json.dumps(payloads, ensure_ascii=False))
    path = root / "atlas/changes.jsonl"
    event = {
        "sequence": 25,
        "external_id": "t01",
        "version": "rephrased-v2",
        "operation": "upsert",
        "changed_at": app.clock.now().isoformat(),
        "backfill": False,
    }
    path.write_text(path.read_text() + json.dumps(event) + "\n")
    app.settings = app.settings.model_copy(update={"fixtures_dir": root})
    await ingest_once(app, "atlas")

    async def rephrased(segments, repair=False):
        return json.dumps(
            {
                "items": [
                    {
                        "kind": "feature_request",
                        "urgency": "normal",
                        "need": "Экспортировать список заказов в CSV",
                        "context": "Синтетический пример поддержки",
                        "evidence": [{"segment_id": "body-1", "quote": "CSV"}],
                        "needs_review": False,
                        "review_reason": None,
                    }
                ]
            }
        ), {}

    async def matched(item, candidates, segments):
        assert gid in {candidate["id"] for candidate in candidates}
        return json.dumps(
            {
                "action": "match",
                "candidate_id": gid,
                "reason": "Controlled semantic match",
                "evidence": [quote.model_dump() for quote in item.evidence],
            }
        ), {}

    app.models.extract, app.models.match = rephrased, matched
    await drain(app)
    with app.db.tx(["atlas"]) as s:
        current = s.scalar(select(Need).where(Need.ticket_id == "demo:t01"))
        assert current.group_id == gid and current.first_seen == first_seen
