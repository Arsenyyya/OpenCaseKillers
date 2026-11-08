import asyncio
import logging
import signal
from contextlib import suppress
from support_agent.errors import AgentError, Transient, Stale
from support_agent.services.media.worker import run_media
from support_agent.services.analysis.worker import run_analysis
from support_agent.services.delivery.worker import run_delivery
from support_agent.services.state import retention_run
from support_agent.db.models import ProductLease
from sqlalchemy import select

log = logging.getLogger("support_agent")
HANDLERS = {"media": run_media, "media_text": run_media, "analysis": run_analysis, "delivery": run_delivery}


async def run_one(app, kind):
    claim = app.queue.claim(kind)
    if not claim:
        return False

    async def renew():
        while True:
            await asyncio.sleep(app.settings.lease_seconds / 3)
            app.queue.heartbeat(claim)
            if kind == "analysis":
                # Assignment lease is renewed only by its actual current token in the analysis handler.
                pass

    heartbeat = asyncio.create_task(renew())
    try:
        if kind == "retention":
            with app.db.tx([claim.product_id]) as s:
                lease = s.scalar(
                    select(ProductLease)
                    .where(ProductLease.product_id == claim.product_id, ProductLease.id == "assignment")
                    .with_for_update()
                )
                lease.token, lease.until = None, None
                app.queue.guard(s, claim)
                retention_run(s, claim.product_id, app.clock.now(), app.settings.retention_days)
                app.queue.complete(s, claim)
        else:
            await HANDLERS[kind](app, claim)
        if heartbeat.done():
            heartbeat.result()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Content, credentials, validation payload and provider errors never enter logs.
        log.warning(
            "job_failed job_id=%s stage=%s error_code=%s",
            claim.id,
            kind,
            getattr(exc, "code", "processing_failed"),
        )
        app.queue.fail(claim, exc if isinstance(exc, AgentError) else Transient())
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError, Stale):
            await heartbeat
    return True


async def drain(app, limit=10000):
    count = 0
    while count < limit:
        progressed = False
        for kind in ("media", "analysis", "delivery", "retention"):
            if await run_one(app, kind):
                count += 1
                progressed = True
        if not progressed:
            return count
    raise RuntimeError("bounded drain limit exceeded")


async def worker_loop(app, kind):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        while not stop.is_set():
            done = await run_one(app, kind)
            if not done:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1)
                except TimeoutError:
                    pass
    finally:
        await app.close()
