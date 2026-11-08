import asyncio
import multiprocessing as mp
from dataclasses import asdict
from datetime import timedelta
import pytest
from sqlalchemy import select, func
from support_agent.application import Application
from support_agent.settings import Settings
from support_agent.clock import FrozenClock
from support_agent.queue import Claim, enqueue
from support_agent.db.models import Job, Group, Membership
from support_agent.errors import Capacity, Stale
from support_agent.services.ingest.service import ingest_once
from support_agent.worker import run_one, drain


def claim_process(config, now, barrier, result):
    app = Application(Settings.model_validate(config), clock=FrozenClock(now))
    try:
        barrier.wait(timeout=20)
        c = app.queue.claim("probe")
        result.put(asdict(c) if c else None)
    finally:
        app.db.close()


def slot_process(config, now, start, held, result):
    app = Application(Settings.model_validate(config), clock=FrozenClock(now))
    slot = None
    try:
        start.wait(timeout=20)
        try:
            slot = app.queue.acquire_slot("llm")
            result.put(True)
        except Capacity:
            result.put(False)
        held.wait(timeout=20)
        if slot:
            app.queue.slot_action(*slot, release=True)
    finally:
        app.db.close()


def analysis_process(config, now, barrier, result):
    app = Application(Settings.model_validate(config), clock=FrozenClock(now))

    async def run():
        original = app.models.extract

        async def delayed(*args, **kw):
            await asyncio.sleep(0.15)
            return await original(*args, **kw)

        app.models.extract = delayed
        barrier.wait(timeout=20)
        return await run_one(app, "analysis")

    try:
        result.put(asyncio.run(run()))
    finally:
        app.db.close()


def processes(target, args, count):
    ctx = mp.get_context("spawn")
    result, barrier = ctx.Queue(), ctx.Barrier(count)
    ps = [ctx.Process(target=target, args=(*args, barrier, result)) for _ in range(count)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(timeout=30)
        if p.is_alive():
            p.terminate()
            raise AssertionError("process failed to finish")
        assert p.exitcode == 0
    return [result.get(timeout=2) for _ in ps]


def test_two_process_claim_and_fencing_after_expiry(app):
    with app.db.tx(["atlas"]) as s:
        enqueue(s, "atlas", "probe", "only-one", app.clock.now())
    values = processes(claim_process, (app.settings.model_dump(), app.clock.now()), 2)
    claims = [Claim(**v) for v in values if v]
    assert len(claims) == 1
    old = claims[0]
    app.clock.value += timedelta(seconds=app.settings.lease_seconds + 1)
    new = app.queue.claim("probe")
    assert old.id == new.id and old.token != new.token
    with app.db.tx(["atlas"]) as s:
        with pytest.raises(Stale):
            app.queue.complete(s, old)
    with pytest.raises(Stale):
        app.queue.heartbeat(old)
    with app.db.tx(["atlas"]) as s:
        app.queue.complete(s, new)


def test_provider_slots_are_shared_between_processes(app):
    ctx = mp.get_context("spawn")
    count = 4
    result, start, held = ctx.Queue(), ctx.Barrier(count), ctx.Barrier(count)
    ps = [
        ctx.Process(
            target=slot_process, args=(app.settings.model_dump(), app.clock.now(), start, held, result)
        )
        for _ in range(count)
    ]
    for p in ps:
        p.start()
    for p in ps:
        p.join(timeout=30)
        assert not p.is_alive() and p.exitcode == 0
    assert sum(result.get(timeout=2) for _ in ps) == app.settings.model_slots


async def test_concurrent_grouping_no_conflicts_and_product_fairness(app):
    await ingest_once(app, "atlas")
    while await run_one(app, "media"):
        pass
    result = await asyncio.to_thread(
        processes, analysis_process, (app.settings.model_dump(), app.clock.now()), 2
    )
    assert result == [True, True]
    app.clock.value += timedelta(seconds=5)
    await drain(app)
    with app.db.tx(["atlas"]) as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(Group)
                .where(Group.need == "Добавить выгрузку списка заказов в CSV")
            )
            == 1
        )
        assert s.scalar(select(func.count()).select_from(Membership)) > 0
    token = app.queue.acquire_product("atlas")
    other = app.queue.acquire_product("boreal")
    with pytest.raises(Capacity):
        app.queue.acquire_product("atlas")
    app.queue.release_product("boreal", other)
    app.clock.value += timedelta(seconds=app.settings.lease_seconds + 1)
    fresh = app.queue.acquire_product("atlas")
    with app.db.tx(["atlas"]) as s:
        with pytest.raises(Stale):
            app.queue.product_guard(s, "atlas", token)
    app.queue.release_product("atlas", fresh)


def test_backfill_priority_and_round_robin(app):
    with app.db.tx(["atlas", "boreal"]) as s:
        enqueue(s, "atlas", "probe", "old", app.clock.now() - timedelta(days=1), priority=1)
        enqueue(s, "atlas", "probe", "fresh", app.clock.now(), priority=10)
        enqueue(s, "boreal", "probe", "other", app.clock.now(), priority=10)
    c1, c2 = app.queue.claim("probe"), app.queue.claim("probe")
    assert c1.product_id == "atlas" and c2.product_id == "boreal"
    with app.db.tx(["atlas"]) as s:
        assert s.get(Job, ("atlas", c1.id)).dedupe_key == "fresh"
