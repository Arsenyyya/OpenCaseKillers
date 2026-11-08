import json
from sqlalchemy import select, func
from support_agent.db.models import (
    Ticket,
    Revision,
    Need,
    Group,
    Membership,
    Card,
    Receipt,
    Job,
    Cursor,
    Outbox,
)
from support_agent.demo import run_demo
from support_agent.services.ingest.service import ingest_once
from support_agent.services.delivery.service import build_digest, record_product_decision
from support_agent.worker import drain
from support_agent.application import Application
from support_agent.auth import authenticate
from support_agent.contracts import DecisionRequest
from support_agent.errors import Conflict, Stale
import pytest

CSV = "Добавить выгрузку списка заказов в CSV"


async def intake(app):
    for p in app.settings.service_products:
        await ingest_once(app, p)
    await drain(app)


def count(s, cls):
    return s.scalar(select(func.count()).select_from(cls))


async def test_full_path_restart_replay_and_counts(app):
    result = await run_demo(app)
    assert result["counts"]["tickets"] == 40
    assert result["counts"]["cards"] == 6
    assert result["counts"]["receipts"] == 2
    assert result["repeat_unchanged"]
    assert result["metrics"]["jobs"].get("dead", 0) == 0
    assert result["metrics"]["jobs"].get("retry_wait", 0) == 0
    fresh = Application(app.settings, clock=app.clock)
    try:
        with fresh.db.tx(["atlas"]) as s:
            assert count(s, Receipt) == 1
            csv = s.scalar(select(Group).where(Group.need == CSV))
            members = s.scalars(select(Membership).where(Membership.group_id == csv.id)).all()
            assert len(members) == 12  # t07 aliases t01; t23 is partial and excluded
            assert count(s, Ticket) == 24
            from support_agent.policy import digest

            assert all(digest(card.snapshot) == card.content_hash for card in s.scalars(select(Card)))
            assert s.get(Revision, ("atlas", "demo:t23:1")).status == "partial"
            assert s.get(Revision, ("atlas", "demo:t20:1")).coverage["complete"] is False
            assert len(s.scalars(select(Need).where(Need.ticket_id == "demo:t14")).all()) == 2
            defects = s.scalar(select(Group).where(Group.kind == "defect"))
            assert defects.id != csv.id
    finally:
        await fresh.close()


async def test_reprocess_same_revision_is_material_noop(app):
    await intake(app)
    build_digest(app, "atlas")
    with app.db.tx(["atlas"]) as s:
        old = {c.id: (c.revision, c.content_hash) for c in s.scalars(select(Card))}
        j = s.scalar(select(Job).where(Job.kind == "analysis", Job.revision_id == "demo:t01:1"))
        j.status, j.attempts = "queued", 0
    await drain(app)
    build_digest(app, "atlas")
    with app.db.tx(["atlas"]) as s:
        assert {c.id: (c.revision, c.content_hash) for c in s.scalars(select(Card))} == old
        assert s.scalar(select(func.count()).select_from(Need).where(Need.revision_id == "demo:t01:1")) == 1


async def test_cursor_batch_transaction_and_replay(app, monkeypatch):
    from support_agent.services.ingest import service

    original = service.enqueue

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic crash before enqueue")

    monkeypatch.setattr(service, "enqueue", fail)
    with pytest.raises(RuntimeError):
        await ingest_once(app, "atlas")
    with app.db.tx(["atlas"]) as s:
        assert count(s, Ticket) == 0
        assert s.get(Cursor, ("atlas", "demo")).position == "0"
    monkeypatch.setattr(service, "enqueue", original)
    await ingest_once(app, "atlas")
    with app.db.tx(["atlas"]) as s:
        assert count(s, Ticket) == 24
        assert count(s, Job) == 24
        s.get(Cursor, ("atlas", "demo")).position = "0"
    await ingest_once(app, "atlas")
    with app.db.tx(["atlas"]) as s:
        assert count(s, Job) == 24
        assert s.get(Ticket, ("atlas", "demo:t01")).current_revision == 1


async def apply_mutations(app, tmp_path):
    import shutil

    root = tmp_path / "fixtures"
    shutil.copytree(app.settings.fixtures_dir, root)
    path = root / "atlas/changes.jsonl"
    path.write_text(path.read_text() + (root / "atlas/mutations.jsonl").read_text())
    app.settings = app.settings.model_copy(update={"fixtures_dir": root})
    await ingest_once(app, "atlas")


async def test_update_delete_cancel_approval_purge_and_no_resurrection(app, tmp_path):
    await intake(app)
    build_digest(app, "atlas")
    with app.db.tx(["atlas"]) as s:
        card = s.scalar(select(Card).where(Card.snapshot["need"].astext == CSV))
        request = DecisionRequest(
            revision=card.revision, content_hash=card.content_hash, decision="accepted", reason="test"
        )
    actor = authenticate(app, "Bearer " + app.tokens["reviewer"])
    record_product_decision(app, actor, "atlas", card.id, request)
    # An in-flight old analysis cannot resurrect a deleted ticket.
    with app.db.tx(["atlas"]) as s:
        j = s.scalar(select(Job).where(Job.kind == "analysis", Job.revision_id == "demo:t03:1"))
        j.status, j.attempts = "queued", 0
    claim = app.queue.claim("analysis")
    await apply_mutations(app, tmp_path)
    with app.db.tx(["atlas"]) as s:
        assert s.get(Ticket, ("atlas", "demo:t03")).deleted
        deleted = s.get(Revision, ("atlas", "demo:t03:1"))
        assert deleted.raw is None and deleted.segments is None
        assert not s.scalars(select(Need).where(Need.ticket_id == "demo:t03")).all()
        assert all(o.status == "cancelled" for o in s.scalars(select(Outbox)))
        with pytest.raises(Stale):
            app.queue.guard(s, claim)
    with pytest.raises(Conflict):
        record_product_decision(app, actor, "atlas", card.id, request)
    await drain(app)
    build_digest(app, "atlas")
    with app.db.tx(["atlas"]) as s:
        csv = s.scalar(select(Group).where(Group.need == CSV))
        assert (
            s.scalar(select(func.count()).select_from(Membership).where(Membership.group_id == csv.id)) == 10
        )
        assert s.get(Ticket, ("atlas", "demo:t02")).source_version == "opaque-new"
        assert s.get(Ticket, ("atlas", "demo:t02")).current_revision == 2
        assert s.get(Card, ("atlas", card.id)).revision > request.revision
    # Replay an older ordered event while moving cursor back; new source sequence wins.
    with app.db.tx(["atlas"]) as s:
        s.get(Cursor, ("atlas", "demo")).position = "0"
    await ingest_once(app, "atlas")
    with app.db.tx(["atlas"]) as s:
        assert s.get(Ticket, ("atlas", "demo:t03")).deleted
        assert s.get(Ticket, ("atlas", "demo:t02")).current_revision == 2


async def test_stage_completion_and_next_enqueue_are_atomic(app, monkeypatch):
    await ingest_once(app, "boreal")
    c = app.queue.claim("media")
    from support_agent.services.media.worker import run_media
    import support_agent.queue as q

    def crash(*args, **kwargs):
        raise RuntimeError("between result and enqueue")

    monkeypatch.setattr(q, "enqueue", crash)
    with pytest.raises(RuntimeError):
        await run_media(app, c)
    with app.db.tx(["boreal"]) as s:
        assert s.get(Revision, ("boreal", c.revision_id)).segments is None
        assert s.get(Job, ("boreal", c.id)).status == "running"
        assert not s.scalars(select(Job).where(Job.kind == "analysis")).all()


async def test_duplicate_graph_updates_recount_descendants_and_reject_cycles(app, tmp_path):
    import shutil
    from support_agent.errors import Permanent

    await intake(app)
    root = tmp_path / "duplicates"
    shutil.copytree(app.settings.fixtures_dir, root)
    app.settings = app.settings.model_copy(update={"fixtures_dir": root})
    path = root / "atlas/changes.jsonl"
    tickets = root / "atlas/tickets.json"
    rows = json.loads(tickets.read_text())
    revised = dict(rows["t01:v1"]) | {"version": "v2", "duplicate_of": "t02"}
    rows["t01:v2"] = revised
    tickets.write_text(json.dumps(rows, ensure_ascii=False))
    event = {
        "sequence": 25,
        "external_id": "t01",
        "version": "v2",
        "operation": "upsert",
        "changed_at": "2026-11-06T12:00:00Z",
        "backfill": False,
    }
    path.write_text(path.read_text() + json.dumps(event) + "\n")
    await ingest_once(app, "atlas")
    await drain(app)
    with app.db.tx(["atlas"]) as s:
        assert s.get(Ticket, ("atlas", "demo:t07")).origin_id == "demo:t02"
        group = s.scalar(select(Group).where(Group.need == CSV))
        assert (
            s.scalar(select(func.count()).select_from(Membership).where(Membership.group_id == group.id))
            == 11
        )
    rows["t02:cycle"] = dict(rows["t02:v1"]) | {"version": "cycle", "duplicate_of": "t07"}
    tickets.write_text(json.dumps(rows, ensure_ascii=False))
    path.write_text(
        path.read_text()
        + json.dumps(event | {"sequence": 26, "external_id": "t02", "version": "cycle"})
        + "\n"
    )
    with pytest.raises(Permanent):
        await ingest_once(app, "atlas")
    with app.db.tx(["atlas"]) as s:
        assert s.get(Cursor, ("atlas", "demo")).position == "25"
        assert s.get(Ticket, ("atlas", "demo:t02")).current_revision == 1


async def test_daily_five_card_limit_persists_across_scheduler_runs(app):
    from datetime import timedelta

    await intake(app)
    with app.db.tx(["atlas"]) as s:
        for i in range(6):
            group = Group(
                product_id="atlas",
                id=f"extra-{i}",
                kind="feature_request",
                need=f"Другая потребность {i}",
                context="",
                first_seen=app.clock.now() - timedelta(days=1),
                provisional=False,
                active=True,
            )
            s.add(group)
            s.flush()
            for n in (1, 2):
                tid = f"demo:t{n:02}"
                s.add(
                    Need(
                        product_id="atlas",
                        group_id=group.id,
                        revision_id=tid + ":1",
                        ticket_id=tid,
                        origin_id=tid,
                        kind="feature_request",
                        need=group.need,
                        context="",
                        urgency="unknown",
                        evidence=[{"segment_id": "body-1", "quote": "CSV"}],
                        eligible=True,
                        first_seen=group.first_seen,
                    )
                )
                s.add(
                    Membership(
                        product_id="atlas", group_id=group.id, origin_id=tid, first_seen=group.first_seen
                    )
                )
    first = build_digest(app, "atlas")
    assert len(first["cards"]) == 5
    second = build_digest(app, "atlas")
    with app.db.tx(["atlas"]) as s:
        assert count(s, Card) == 5
    assert second["cards"] == first["cards"]
    # Removing one daily card must retain the other four valid references.
    from sqlalchemy import delete
    from support_agent.db.models import Digest

    with app.db.tx(["atlas"]) as s:
        removed = s.get(Card, ("atlas", first["cards"][0]["card_id"]))
        s.get(Group, ("atlas", removed.group_id)).active = False
        removed.status = "stale"
        s.execute(delete(Digest).where(Digest.product_id == "atlas"))
    assert len(build_digest(app, "atlas")["cards"]) == 4
    app.clock.value += timedelta(days=1)
    next_day = build_digest(app, "atlas")
    assert 0 < len(next_day["cards"]) <= 5
    with app.db.tx(["atlas"]) as s:
        assert count(s, Card) > 5
