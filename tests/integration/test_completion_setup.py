import asyncio
import json
import shutil
from datetime import timedelta

import pytest
from sqlalchemy import select, func
from pydantic import ValidationError

from support_agent.catalog import lookup_known_task
from support_agent.db.models import Ticket, Job, Grant, Cursor, ProviderSlot
from support_agent.db.session import Database
from support_agent.diagnostics import doctor
from support_agent.errors import Permanent, Transient, ConfigurationError
from support_agent.provisioning import ProvisionSpec, provision_registry
from support_agent.services.ingest.service import ingest_once


def spec_for_atlas():
    return ProvisionSpec.model_validate(
        {
            "products": [
                {
                    "id": "atlas",
                    "owner": "reviewer",
                    "sources": ["demo", "gateway"],
                    "terms": {"CSV": "Табличная выгрузка"},
                    "known_tasks": [
                        {
                            "id": "SYNTH-1",
                            "kind": "feature_request",
                            "need": "Добавить CSV",
                            "status": "planned",
                            "reference": "SYNTH-1",
                        }
                    ],
                }
            ],
            "principals": [
                {"id": "reviewer", "bindings": [{"product_id": "atlas", "roles": ["product_reviewer"]}]},
                {"id": "operator", "bindings": [{"product_id": "atlas", "roles": ["operator"]}]},
            ],
            "provider_slots": {"llm": 2, "asr": 1, "vl": 1},
        }
    )


def test_registry_is_idempotent_dry_run_and_does_not_reset_cursors(app):
    spec = spec_for_atlas()
    dry = provision_registry(spec, None, dry_run=True)
    assert dry["dry_run"] and dry["products"] == ["atlas"]
    with app.db.tx(["atlas"]) as s:
        s.get(Cursor, ("atlas", "gateway")).position = "opaque-cursor"
    for _ in range(2):
        provision_registry(spec, app.settings.database_admin_url, clock=app.clock)
    with app.db.tx(["atlas"]) as s:
        assert s.get(Cursor, ("atlas", "gateway")).position == "opaque-cursor"
        assert s.scalar(select(func.count()).select_from(Grant)) == 3
        assert (
            s.scalar(select(func.count()).select_from(Grant).where(Grant.actor.in_(["reviewer", "operator"])))
            == 2
        )
        assert lookup_known_task(s, "atlas", "feature_request", " добавить CSV ")["id"] == "SYNTH-1"
        assert lookup_known_task(s, "atlas", "defect", "Добавить CSV") is None
    assert doctor(app.settings)["ready"]


def test_registry_bad_scope_is_rejected_and_busy_capacity_cannot_be_removed(app):
    value = spec_for_atlas().model_dump()
    value["principals"][0]["bindings"][0]["product_id"] = "other-product"
    with pytest.raises(ValidationError):
        ProvisionSpec.model_validate(value)
    admin = Database(app.settings.database_admin_url)
    try:
        with admin.tx() as s:
            slot = s.get(ProviderSlot, "asr:1")
            slot.token = "active-lease"
            slot.until = app.clock.now() + timedelta(seconds=90)
    finally:
        admin.close()
    with pytest.raises(ConfigurationError):
        provision_registry(spec_for_atlas(), app.settings.database_admin_url, clock=app.clock)
    with app.db.tx(["atlas"]) as s:
        assert s.get(Cursor, ("atlas", "gateway")).position == "0"


async def test_new_feed_sequence_same_source_version_is_noop_and_immutable(app, tmp_path):
    await ingest_once(app, "atlas")
    root = tmp_path / "fixtures"
    shutil.copytree(app.settings.fixtures_dir, root)
    app.settings = app.settings.model_copy(update={"fixtures_dir": root})
    with app.db.tx(["atlas"]) as s:
        before = s.get(Ticket, ("atlas", "demo:t01"))
        accepted_revision, updated_at = before.current_revision, before.updated_at
        jobs = s.scalar(select(func.count()).select_from(Job))
    path = root / "atlas/changes.jsonl"
    event = {
        "sequence": 25,
        "external_id": "t01",
        "version": "v1",
        "operation": "upsert",
        "changed_at": "2026-11-06T12:00:00Z",
        "backfill": False,
    }
    path.write_text(path.read_text() + json.dumps(event) + "\n")
    assert (await ingest_once(app, "atlas"))["accepted"] == 0
    with app.db.tx(["atlas"]) as s:
        ticket = s.get(Ticket, ("atlas", "demo:t01"))
        assert ticket.current_revision == accepted_revision and ticket.updated_at == updated_at
        assert ticket.source_seq == 25 and s.scalar(select(func.count()).select_from(Job)) == jobs
    tickets = root / "atlas/tickets.json"
    rows = json.loads(tickets.read_text())
    rows["t01:v1"]["text"] = "Changed without changing the immutable source version"
    tickets.write_text(json.dumps(rows))
    path.write_text(path.read_text() + json.dumps(event | {"sequence": 26}) + "\n")
    with pytest.raises(Permanent):
        await ingest_once(app, "atlas")
    with app.db.tx(["atlas"]) as s:
        assert s.get(Cursor, ("atlas", "demo")).position == "25"
        assert s.get(Ticket, ("atlas", "demo:t01")).current_revision == accepted_revision


async def test_model_heartbeat_failure_cancels_call_and_releases_slot(app):
    await ingest_once(app, "atlas")
    app.settings = app.settings.model_copy(update={"lease_seconds": 2})
    claim = app.queue.claim("media")
    original = app.queue.slot_action
    cancelled = asyncio.Event()

    def failed_renewal(slot_id, token, release=False):
        if not release:
            raise RuntimeError("synthetic DB renewal failure")
        return original(slot_id, token, release=True)

    app.queue.slot_action = failed_renewal

    async def slow_call():
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    with pytest.raises(Transient):
        await app.model_call(claim, "llm", slow_call)
    assert cancelled.is_set()
    with app.db.tx() as s:
        assert not s.scalars(
            select(ProviderSlot).where(ProviderSlot.provider == "llm", ProviderSlot.token.is_not(None))
        ).all()


async def test_metrics_use_aggregates_without_selecting_content(app):
    from support_agent.observability.metrics import report
    from sqlalchemy import event

    await ingest_once(app, "atlas")
    queries = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(app.db.engine, "before_cursor_execute", capture)
    try:
        result = report(app, ["atlas"])
    finally:
        event.remove(app.db.engine, "before_cursor_execute", capture)
    assert sum(ch["available_in_ingested_scope"] for ch in result["channels"].values()) == 24
    assert sum(ch["fully_processed"] for ch in result["channels"].values()) == 0
    assert result["reported_tokens"] == {"input_tokens": None, "output_tokens": None}
    assert all("revisions.raw" not in q and "cards.snapshot" not in q for q in queries)
