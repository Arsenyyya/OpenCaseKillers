import asyncio
import logging
import signal
from contextlib import suppress
from uuid import uuid4

from sqlalchemy import select

from support_agent.db.models import ProductLease
from support_agent.errors import AgentError, Stale, Transient
from support_agent.queue import enqueue
from support_agent.services.analysis.worker import run_analysis
from support_agent.services.delivery.worker import run_delivery
from support_agent.services.media.worker import run_media
from support_agent.services.state import retention_plan, retention_run

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

    async def execute():
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
                if retention_plan(s, claim.product_id, app.clock.now(), app.settings.retention_days, limit=1)[
                    "total_revisions"
                ]:
                    enqueue(
                        s, claim.product_id, "retention", f"retention:{uuid4()}", app.clock.now(), priority=1
                    )
                app.queue.complete(s, claim)
        else:
            await HANDLERS[kind](app, claim)

    heartbeat = asyncio.create_task(renew())
    operation = asyncio.create_task(execute())
    try:
        done, _ = await asyncio.wait([operation, heartbeat], return_when=asyncio.FIRST_COMPLETED)
        # Completion may race with the next heartbeat observing the now-succeeded job.
        if operation in done:
            operation.result()
        else:
            heartbeat.result()
            raise Stale()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning(
            "job_failed job_id=%s stage=%s error_code=%s",
            claim.id,
            kind,
            getattr(exc, "code", "processing_failed"),
        )
        if not operation.done():
            operation.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await operation
        app.queue.fail(claim, exc if isinstance(exc, AgentError) else Transient())
    finally:
        for task in (operation, heartbeat):
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
    return True


async def drain(app, limit=10000, *, kind=None):
    """Drain ready work, optionally only one explicitly selected stage."""
    count = 0
    kinds = [kind] if kind else ("media", "analysis", "delivery", "retention")
    while count < limit:
        progressed = False
        for selected in kinds:
            if await run_one(app, selected):
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
            try:
                done = await run_one(app, kind)
            except Exception:
                # A temporary database/claim failure does not silently terminate the service.
                log.warning("worker_poll_failed stage=%s error_code=database_or_queue_unavailable", kind)
                done = False
            if not done:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1)
                except TimeoutError:
                    pass
    finally:
        await app.close()
