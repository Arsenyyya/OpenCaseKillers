"""Bounded multiprocess queue/capacity benchmark in a disposable PostgreSQL database."""

import multiprocessing as mp
import time
from datetime import timedelta
from sqlalchemy import select, func
from support_agent.settings import Settings
from support_agent.db.testing import isolated_database
from support_agent.application import Application
from support_agent.db.models import Job
from support_agent.demo import seed_demo
from support_agent.queue import enqueue
from support_agent.errors import Capacity, Transient


def _consumer(config, begin, results, active, peak, lock):
    app = Application(Settings.model_validate(config))
    total, capacity_waits, failures = 0, 0, 0
    deadline = time.monotonic() + 120
    try:
        while time.monotonic() < deadline:
            c = app.queue.claim("load_probe")
            if not c:
                with app.db.tx(app.settings.service_products) as s:
                    remaining = s.scalar(
                        select(func.count())
                        .select_from(Job)
                        .where(Job.kind == "load_probe", Job.status.in_(["queued", "running", "retry_wait"]))
                    )
                if not remaining:
                    break
                time.sleep(0.01)
                continue
            slot = None
            try:
                slot = app.queue.acquire_slot("llm")
            except Capacity as exc:
                capacity_waits += 1
                app.queue.fail(c, exc)
                continue
            try:
                with lock:
                    active.value += 1
                    peak.value = max(peak.value, active.value)
                if failures == 0:
                    failures += 1
                    app.queue.fail(c, Transient())
                    continue
                time.sleep(0.004)  # Synthetic latency, never presented as GPU performance.
                with app.db.tx([c.product_id]) as s:
                    app.queue.complete(s, c)
                total += 1
            finally:
                with lock:
                    active.value -= 1
                app.queue.slot_action(*slot, release=True)
        results.put({"completed": total, "capacity_waits": capacity_waits, "transient_failures": failures})
    finally:
        app.db.close()


def load_test(tickets=300, workers=4):
    if not 10 <= tickets <= 3000 or not 2 <= workers <= 8:
        raise ValueError("bounded load test: 10..3000 tickets, 2..8 workers")
    settings = Settings()
    with isolated_database(settings, prefix="agent_load_") as cfg:
        app = Application(cfg)
        seed_demo(app)
        now = app.clock.now()
        with app.db.tx(cfg.service_products) as s:
            for i in range(tickets):
                enqueue(
                    s,
                    cfg.service_products[i % len(cfg.service_products)],
                    "load_probe",
                    f"load:{i}",
                    now - timedelta(days=1),
                    priority=1 if i % 4 == 0 else 10,
                )
        ctx = mp.get_context("spawn")
        results, active, peak, lock = ctx.Queue(), ctx.Value("i", 0), ctx.Value("i", 0), ctx.Lock()
        begin = time.monotonic()
        ps = [
            ctx.Process(target=_consumer, args=(cfg.model_dump(), begin, results, active, peak, lock))
            for _ in range(workers)
        ]
        for p in ps:
            p.start()
        for p in ps:
            p.join(timeout=125)
            if p.is_alive():
                p.terminate()
                p.join(timeout=2)
                raise RuntimeError("benchmark time budget exceeded")
            if p.exitcode != 0:
                raise RuntimeError("benchmark subprocess failed")
        measurements = [results.get(timeout=2) for _ in ps]
        elapsed = time.monotonic() - begin
        with app.db.tx(cfg.service_products) as s:
            statuses = dict(s.execute(select(Job.status, func.count()).group_by(Job.status)).all())
        app.db.close()
        return {
            "synthetic": True,
            "scope": "PostgreSQL queue, fairness, retry and shared slots; no Qwen or end-to-end media benchmark",
            "tickets": tickets,
            "workers": workers,
            "provider_limit": cfg.model_slots,
            "peak_provider_calls": peak.value,
            "elapsed_seconds": round(elapsed, 3),
            "status_counts": statuses,
            "worker_results": measurements,
            "all_completed": statuses.get("succeeded", 0) == tickets,
            "illustrative_profile": {
                "tickets_per_day": 1000,
                "threefold": 3000,
                "audio_share": 0.2,
                "minutes_per_call": 5,
                "image_pages_per_day": 100,
            },
            "media_profile_executed": False,
            "real_24h_freshness_slo": "not_run",
        }
