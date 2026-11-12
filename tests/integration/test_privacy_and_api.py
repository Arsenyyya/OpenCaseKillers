"""Privacy regressions are checked across historical copies, not only current Needs."""

from datetime import timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from dotenv import dotenv_values

from support_agent.api import create_app
from support_agent.application import Application
from support_agent.db.session import Database
from support_agent.db.models import (
    Ticket,
    Revision,
    Need,
    Group,
    Card,
    Digest,
    Decision,
    Outbox,
    Receipt,
    Membership,
    Observation,
    GroupObservation,
    MediaCache,
    Grant,
    Metric,
)
from support_agent.services.state import remove_ticket_content, retention_plan, retention_run
from support_agent.policy import digest


def history(app, *, old_days=100):
    """A ticket left the old group, but delivered snapshots still cite its first version."""
    p, now = "atlas", app.clock.now()
    with app.db.tx([p]) as s:
        for tid, current in [("history", 2), ("survivor", 1)]:
            s.add(
                Ticket(
                    product_id=p,
                    id=tid,
                    source_id="demo",
                    external_id=tid,
                    source_seq=current,
                    source_version=f"v{current}",
                    current_revision=current,
                    origin_id=tid,
                    deleted=False,
                    occurred_at=now - timedelta(days=5),
                    updated_at=now,
                    channel="text",
                )
            )
        for gid in ["old", "new"]:
            s.add(
                Group(
                    product_id=p,
                    id=gid,
                    kind="feature_request",
                    need=f"{gid} need",
                    context="",
                    first_seen=now - timedelta(days=5),
                    provisional=False,
                    active=True,
                )
            )
        s.flush()
        for rid, tid, num, created, quote in [
            ("history:1", "history", 1, now - timedelta(days=old_days), "old confidential evidence"),
            ("history:2", "history", 2, now, "fresh confidential evidence"),
            ("survivor:1", "survivor", 1, now, "permitted surviving evidence"),
        ]:
            s.add(
                Revision(
                    product_id=p,
                    id=rid,
                    ticket_id=tid,
                    number=num,
                    source_version=f"v{num}",
                    content_hash=digest([rid, quote]),
                    raw={"text": quote},
                    segments=[{"segment_id": "s", "text": quote}],
                    extracted_items=[{"need": quote}],
                    status="processed",
                    coverage={"complete": True},
                    created_at=created,
                )
            )
        s.flush()
        for tid, rid, gid, quote in [
            ("history", "history:2", "new", "fresh confidential evidence"),
            ("survivor", "survivor:1", "old", "permitted surviving evidence"),
        ]:
            s.add(
                Need(
                    product_id=p,
                    ticket_id=tid,
                    revision_id=rid,
                    group_id=gid,
                    origin_id=tid,
                    kind="feature_request",
                    need=f"{gid} need",
                    context="",
                    urgency="unknown",
                    evidence=[{"segment_id": "s", "quote": quote}],
                    eligible=True,
                    first_seen=now - timedelta(days=5),
                )
            )
            s.add(Membership(product_id=p, group_id=gid, origin_id=tid, first_seen=now - timedelta(days=5)))
        for gid in ["old", "new"]:
            s.add(
                GroupObservation(
                    product_id=p, ticket_id="history", group_id=gid, first_seen=now - timedelta(days=5)
                )
            )
        s.add(
            Observation(
                product_id=p, ticket_id="history", need_hash=digest("old"), first_seen=now - timedelta(days=5)
            )
        )
        s.add(
            MediaCache(
                product_id=p,
                revision_id="history:1",
                content_hash=digest("cache"),
                processor_version="test",
                settings_hash=digest("test"),
                segments=[{"text": "old confidential evidence"}],
                created_at=now,
            )
        )
        old = {
            "group_id": "old",
            "need": "old need",
            "sources": [
                {
                    "ticket_id": "history",
                    "revision_id": "history:1",
                    "evidence": [{"segment_id": "s", "quote": "old confidential evidence"}],
                }
            ],
        }
        fresh = {
            "group_id": "new",
            "need": "new need",
            "sources": [
                {
                    "ticket_id": "history",
                    "revision_id": "history:2",
                    "evidence": [{"segment_id": "s", "quote": "fresh confidential evidence"}],
                }
            ],
        }
        s.add(
            Card(
                product_id=p,
                id="old-card",
                group_id="old",
                revision=1,
                content_hash=digest(old),
                snapshot=old,
                status="delivered",
                created_at=now,
            )
        )
        s.add(
            Card(
                product_id=p,
                id="new-card",
                group_id="new",
                revision=1,
                content_hash=digest(fresh),
                snapshot=fresh,
                status="draft",
                created_at=now,
            )
        )
        s.flush()
        s.add(
            Decision(
                product_id=p,
                id="old-approval",
                card_id="old-card",
                revision=1,
                content_hash=digest(old),
                actor="reviewer",
                decision="accepted",
                reason="old confidential evidence",
                created_at=now,
            )
        )
        s.flush()
        s.add(
            Outbox(
                product_id=p,
                id="old-out",
                card_id="old-card",
                decision_id="old-approval",
                revision=1,
                content_hash=digest(old),
                correlation_key="atlas:old-card:1",
                status="delivered",
                external_id="external",
                sent_at=now,
                resolution_reason="old confidential evidence",
            )
        )
        s.add(
            Receipt(
                product_id=p,
                id="old-receipt",
                correlation_key="atlas:old-card:1",
                payload=old,
                created_at=now,
            )
        )
        s.add(
            Digest(
                product_id=p,
                id="old-digest",
                period="2026-11-06",
                snapshot=[old],
                content_hash=digest([old]),
                created_at=now,
            )
        )


def test_delete_erases_historical_snapshot_after_ticket_left_group(app):
    history(app)
    with app.db.tx(["atlas"]) as s:
        remove_ticket_content(s, s.get(Ticket, ("atlas", "history")), app.clock.now())
    with app.db.tx(["atlas"]) as s:
        assert s.get(Card, ("atlas", "old-card")).snapshot == {}
        assert s.get(Card, ("atlas", "old-card")).status == "correction_required"
        assert s.get(Receipt, ("atlas", "old-receipt")).payload == {"withdrawal_required": True}
        assert s.get(Outbox, ("atlas", "old-out")).resolution_reason is None
        assert s.get(Decision, ("atlas", "old-approval")).reason == "[content removed]"
        assert not s.scalars(select(Digest)).all()
        for rid in ["history:1", "history:2"]:
            r = s.get(Revision, ("atlas", rid))
            assert r.raw is None and r.segments is None and r.extracted_items is None
        assert s.scalar(select(func.count()).select_from(MediaCache)) == 0
        assert (
            s.scalar(
                select(func.count())
                .select_from(GroupObservation)
                .where(GroupObservation.ticket_id == "history")
            )
            == 0
        )
        assert s.get(Group, ("atlas", "old")).need == "old need"
        assert s.get(Group, ("atlas", "new")).need == "[removed]"


def test_hard_retention_removes_old_revision_of_fresh_ticket(app):
    history(app)
    with app.db.tx(["atlas"]) as s:
        plan = retention_plan(s, "atlas", app.clock.now(), 90, limit=1)
        assert plan["revision_ids"] == ["history:1"] and plan["total_revisions"] == 1
        assert s.get(Revision, ("atlas", "history:1")).raw
        assert retention_run(s, "atlas", app.clock.now(), 90, limit=1) == ["history"]
    with app.db.tx(["atlas"]) as s:
        assert not s.get(Ticket, ("atlas", "history")).deleted
        assert s.get(Revision, ("atlas", "history:2")).raw
        assert s.get(Revision, ("atlas", "history:2")).extracted_items
        assert s.get(Revision, ("atlas", "history:1")).raw is None
        assert s.get(Card, ("atlas", "old-card")).snapshot == {}
        assert s.get(Card, ("atlas", "new-card")).snapshot["sources"][0]["revision_id"] == "history:2"
        assert retention_plan(s, "atlas", app.clock.now(), 90)["total_revisions"] == 0


def test_retention_batches_are_bounded_and_repeat_has_no_content(app):
    history(app, old_days=100)
    with app.db.tx(["atlas"]) as s:
        for r in s.scalars(select(Revision)):
            r.created_at = app.clock.now() - timedelta(days=100)
    with app.db.tx(["atlas"]) as s:
        plan = retention_plan(s, "atlas", app.clock.now(), 90, limit=1)
        assert len(plan["revision_ids"]) == 1 and plan["has_more"]
        with pytest.raises(ValueError):
            retention_plan(s, "atlas", app.clock.now(), 90, limit=501)
        retention_run(s, "atlas", app.clock.now(), 90, limit=1)
    with app.db.tx(["atlas"]) as s:
        assert retention_plan(s, "atlas", app.clock.now(), 90)["total_revisions"] > 0
        retention_run(s, "atlas", app.clock.now(), 90, limit=1)
    with app.db.tx(["atlas"]) as s:
        retention_run(s, "atlas", app.clock.now(), 90, limit=1)
        assert retention_plan(s, "atlas", app.clock.now(), 90)["total_revisions"] == 0
        assert all(r.raw is None and r.segments is None for r in s.scalars(select(Revision)))


def test_api_hides_stale_evidence_and_paginates_current_segments(app):
    history(app)
    with app.db.tx(["atlas"]) as s:
        s.get(Revision, ("atlas", "history:2")).segments = [
            {"segment_id": f"s{i}", "text": f"allowed segment {i}"} for i in range(25)
        ]
    api_url = make_url(dotenv_values(".env")["API_DATABASE_URL"]).set(
        database=make_url(app.settings.database_url).database
    )
    api = Application(app.settings, db=Database(api_url), clock=app.clock)
    try:
        with TestClient(create_app(api)) as client:
            headers = {"Authorization": "Bearer " + app.tokens["viewer"]}
            assert client.get("/products/atlas/evidence/history:1", headers=headers).status_code == 404
            page = client.get("/products/atlas/evidence/history:2?limit=3&offset=3", headers=headers)
            assert page.status_code == 200 and page.json()["total_segments"] == 25
            assert [s["segment_id"] for s in page.json()["segments"]] == ["s3", "s4", "s5"]
            assert page.json()["next_offset"] == 6
            assert (
                client.get("/products/atlas/evidence/history:2?limit=21", headers=headers).status_code == 422
            )
            assert client.get("/products/atlas/digest?period=2026-99-99", headers=headers).status_code == 422
            assert client.get("/products/atlas,boreal/cards", headers=headers).status_code == 422
            op = {"Authorization": "Bearer " + app.tokens["operator"]}
            dry = client.post("/products/atlas/retention?limit=1", headers=op)
            assert dry.status_code == 200 and dry.json()["revision_ids"] == ["history:1"]
            assert (
                client.post("/products/atlas/retention?dry_run=false&offset=1", headers=op).status_code == 409
            )
    finally:
        api.db.close()


def test_worker_cannot_edit_authority_or_capacity_definition(app):
    for sql in [
        "UPDATE grants SET role='operator' WHERE actor='reviewer'",
        "UPDATE products SET owner='operator' WHERE id='atlas'",
        "DELETE FROM provider_slots",
    ]:
        with pytest.raises(DBAPIError):
            with app.db.tx(["atlas"]) as s:
                s.execute(text(sql))
    for products in [["atlas,boreal"], ["atlas\n"], [""]]:
        with pytest.raises(ValueError):
            with app.db.tx(products):
                pass


def test_metrics_scopes_to_operator_bindings_for_mixed_role_actor(app):
    admin = Database(app.settings.database_admin_url)
    try:
        with admin.tx() as s:
            s.add(Grant(product_id="boreal", actor="viewer", role="operator"))
            s.add(
                Metric(product_id="boreal", name="forbidden_operations", value=4, created_at=app.clock.now())
            )
            s.add(
                Metric(product_id="atlas", name="forbidden_operations", value=100, created_at=app.clock.now())
            )
        with TestClient(create_app(app)) as client:
            response = client.get("/metrics", headers={"Authorization": "Bearer " + app.tokens["viewer"]})
            assert response.status_code == 200
            assert response.json()["provider"]["forbidden_operations"] == 4
    finally:
        admin.close()


def test_review_queue_includes_partial_item_inside_qualified_group(app):
    history(app)
    now = app.clock.now()
    with app.db.tx(["atlas"]) as s:
        s.add(
            Ticket(
                product_id="atlas",
                id="ambiguous",
                source_id="demo",
                external_id="ambiguous",
                source_seq=1,
                source_version="v1",
                current_revision=1,
                origin_id="ambiguous",
                deleted=False,
                occurred_at=now,
                updated_at=now,
                channel="text",
            )
        )
        s.flush()
        s.add(
            Revision(
                product_id="atlas",
                id="ambiguous:1",
                ticket_id="ambiguous",
                number=1,
                source_version="v1",
                content_hash=digest("ambiguous"),
                raw={"text": "unclear request"},
                segments=[{"segment_id": "s", "text": "unclear request"}],
                coverage={"complete": True},
                status="needs_review",
                created_at=now,
            )
        )
        s.flush()
        s.add(
            Need(
                product_id="atlas",
                group_id="old",
                ticket_id="ambiguous",
                revision_id="ambiguous:1",
                origin_id="ambiguous",
                kind="feature_request",
                need="old need",
                context="",
                urgency="unknown",
                evidence=[{"segment_id": "s", "quote": "unclear request"}],
                eligible=False,
                needs_review=True,
                review_reason="Ambiguous request",
                first_seen=now,
            )
        )
    with TestClient(create_app(app)) as client:
        headers = {"Authorization": "Bearer " + app.tokens["viewer"]}
        result = client.get("/products/atlas/groups?needs_review=true", headers=headers)
        assert result.status_code == 200
        assert len(result.json()) == 1
        group = result.json()[0]
        assert group["id"] == "old" and group["requires_review"] and not group["provisional"]
        assert group["independent_origins"] == 1
        normal = client.get("/products/atlas/groups?needs_review=false", headers=headers)
        assert [g["id"] for g in normal.json()] == ["new"]
        detail = client.get("/products/atlas/groups/old", headers=headers)
        assert detail.status_code == 200
        assert any(n["review_reason"] == "Ambiguous request" for n in detail.json()["needs"])
